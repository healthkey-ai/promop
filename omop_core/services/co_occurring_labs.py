"""Other results in the same panel or order as a source code (#2702).

A curator deciding what "m-spike quantitation, serum" is wants to know what it
was resulted with: albumin and the alpha, beta and gamma fractions in the same
order say electrophoresis. That link is in the source records, as an
Observation's ``basedOn`` reference or a DiagnosticReport whose ``result``
lists it, and nowhere in the OMOP rows.

The evidence is the per-patient inventory (``PatientSourceCode``), which keeps
**one sample resource per patient and code**. Two codes are seen together only
when a patient's samples for both come from the same order, so this finds
co-occurrence and never proves its absence.

Rules, all per patient (a reference means nothing across patients):

* An order is a ``basedOn`` reference. A display with no reference names an
  order but cannot join two records, so it is not one.
* A record that names no order is joined through a DiagnosticReport in the
  patient's inventory whose ``result`` lists it: the report's other results
  are in the same panel. A record that names its order is not looked up in
  reports, because that lookup reads every report the patient has.
* One Observation is one result however many codings it carries. The codings
  of the record the dialog is open on are left out, and a co-occurring
  Observation is listed once, under the coding in the opened code's own code
  system when it has one.
"""
import json
from collections import Counter, defaultdict

from django.db import OperationalError, connection, transaction

from omop_core.mapping import loinc_axes
from omop_core.services.source_vocabularies import canonical_source_vocabulary

# Inventory records of the opened code that are read. Every one costs a walk
# of that patient's inventory -- about 4,000 rows of stored resources each on
# HealthTree, near 20 ms a patient -- so this bounds the request.
MAX_RECORDS = 30
MAX_CODES = 50
MAX_ORDER_NAMES = 8
# Per statement; a walk that outruns it is reported, not waited for.
STATEMENT_TIMEOUT_MS = 5000
# Where a result is filed. Reports, orders and medications are half of an
# inventory and sit under other tables: results are looked for in these two
# tables only, and reports only outside them, so neither walk reads it all.
RESULT_TABLES = ('measurement', 'observation')

_RESOURCE = '''CROSS JOIN LATERAL (SELECT CASE
        WHEN jsonb_typeof(sc.source_metadata->'fhir') = 'object'
        THEN sc.source_metadata->'fhir' ELSE sc.source_metadata END AS res) r'''
# jsonpath's lax mode unwraps an array and yields nothing for anything else,
# so a malformed basedOn is "no order" rather than an error.
_HAS_ORDER = '''jsonb_path_exists(r.res,
        '$.basedOn[*].reference ? (@.type() == "string" && @ != "")')'''
_COLUMNS = '''sc.id, sc.person_id, sc.source_value, sc.source_vocabulary_id, sc.omop_table,
        COALESCE(sc.source_unit_override, sc.source_unit),
        r.res->>'id', r.res->>'resourceType', r.res->'code', r.res->'basedOn', r.res->'result' '''


class _Record:
    """One inventory row, reduced to what joins it to an order."""

    __slots__ = ('pk', 'person_id', 'code', 'vocabulary', 'omop_table', 'unit',
                 'resource_id', 'resource_type', 'codings', 'text', 'orders', 'results')

    def __init__(self, row):
        (self.pk, self.person_id, self.code, vocabulary, self.omop_table, unit,
         resource_id, resource_type, code, based_on, result) = row
        self.vocabulary = canonical_source_vocabulary(vocabulary)
        self.unit = (unit or '').strip()
        self.resource_id = (resource_id or '').strip()
        self.resource_type = resource_type or ''
        code = _json(code)
        code = code if isinstance(code, dict) else {}
        self.text = code['text'].strip() if isinstance(code.get('text'), str) else ''
        self.codings = [c for c in code.get('coding') or [] if isinstance(c, dict)] \
            if isinstance(code.get('coding'), list) else []
        # {reference: display}, in the resource's order.
        self.orders = {}
        for reference in _list(based_on):
            if isinstance(reference, dict) and isinstance(reference.get('reference'), str) \
                    and reference['reference'].strip():
                display = reference.get('display')
                # Kept as stored: the walk matches it exactly.
                self.orders.setdefault(reference['reference'],
                                       display.strip() if isinstance(display, str) else '')
        self.results = {
            _reference_id(reference.get('reference')) for reference in _list(result)
            if isinstance(reference, dict)
        } - {''}

    @property
    def coding_codes(self):
        return {str(c['code']).strip() for c in self.codings if c.get('code')}

    def description(self):
        """What the source calls this code: the Observation's text, which names
        the test where a shared display does not (#1778), else the display of
        the coding that is this code."""
        if self.text:
            return self.text
        for coding in self.codings:
            if str(coding.get('code') or '').strip() == self.code and isinstance(coding.get('display'), str):
                return coding['display'].strip()
        return ''

    def hospital_loinc(self):
        """The LOINC the hospital sent on this record, when it sent one."""
        if loinc_axes.is_loinc(self.vocabulary):
            return self.code
        for coding in self.codings:
            if loinc_axes.is_loinc(coding.get('system')) and coding.get('code'):
                return str(coding['code']).strip()
        return ''


def _json(value):
    # A raw cursor returns jsonb as text on PostgreSQL; tolerate decoded values.
    return json.loads(value) if isinstance(value, str) else value


def _list(value):
    value = _json(value)
    return value if isinstance(value, list) else []


def _reference_id(reference):
    """``Observation/abc`` and ``https://host/fhir/Observation/abc`` are ``abc``."""
    if not isinstance(reference, str):
        return ''
    return reference.strip().rstrip('/').rsplit('/', 1)[-1]


def _same_result(record, target):
    """Whether *record* is another coding of the record the dialog is open on.

    Resource ids decide when both records carry one. Without them, a code the
    opened record lists among its own codings is that record's alternate
    coding, and so is a record that lists the opened code among its own.
    """
    if record.vocabulary == target.vocabulary and record.code == target.code:
        return True
    if record.resource_id and target.resource_id:
        return record.resource_id == target.resource_id
    return record.code in target.coding_codes or target.code in record.coding_codes


def _result_key(record):
    if record.resource_id:
        return ('id', record.resource_id)
    codes = frozenset(
        (str(c.get('system') or ''), str(c.get('code') or '')) for c in record.codings if c.get('code'))
    return ('codings', codes) if codes else ('row', record.pk)


def _primary(records, vocabulary):
    """The one record that stands for an Observation carrying several codings:
    the opened code's own code system first, then a local code before the
    LOINC beside it (which the table shows as the hospital's), then the order
    the resource lists them in."""
    def rank(record):
        listed = [str(c.get('code') or '').strip() for c in record.codings]
        position = listed.index(record.code) if record.code in listed else len(listed)
        return (record.vocabulary != vocabulary, loinc_axes.is_loinc(record.vocabulary),
                position, record.vocabulary, record.code)
    return min(records, key=rank)


def _walk(cursor, tables, where, wanted):
    """The rows of the patients in *wanted* that match *where*.

    *wanted* is ``{person_id: {values}}``. *where* reads a patient's values
    as ``o.vals`` and, wrapped as the reference list a resource would hold,
    as ``o.refs``. A join, so each row's stored resource is read once:
    reading it is the cost here, and a subquery per row reads it per value.
    """
    pairs = sorted((person, value) for person, values in wanted.items() for value in values)
    cursor.execute(f'''
        SELECT {_COLUMNS}
        FROM (SELECT u.person_id, array_agg(u.value) AS vals,
                     array_agg(jsonb_build_array(jsonb_build_object('reference', u.value))) AS refs
              FROM unnest(%s::int[], %s::text[]) AS u(person_id, value) GROUP BY u.person_id) o
        JOIN patient_source_code sc ON sc.person_id = o.person_id AND {tables}
        {_RESOURCE}
        WHERE {where}''', [[p for p, _ in pairs], [v for _, v in pairs], list(RESULT_TABLES)])  # nosec B608
    return [_Record(row) for row in cursor.fetchall()]


def _targets(cursor, mapping):
    """``(records, total, with_order)`` for the opened code: the records read,
    how many the inventory holds, and how many of those name an order."""
    cursor.execute(
        'SELECT DISTINCT source_vocabulary_id FROM patient_source_code WHERE source_value = %s',
        [mapping.source_code])
    wanted = canonical_source_vocabulary(mapping.source_vocabulary_id)
    # The ETL posts the system it received; the mapping stores the canonical id.
    vocabularies = [v for (v,) in cursor.fetchall() if canonical_source_vocabulary(v) == wanted]
    if not vocabularies:
        return [], 0, 0
    where = ['sc.source_value = %s', 'sc.source_vocabulary_id = ANY(%s)', 'sc.omop_table = ANY(%s)']
    params = [mapping.source_code, vocabularies, sorted({mapping.omop_table or '', ''})]
    if mapping.organization_id:
        # A hospital's mapping is never read against another hospital's patients.
        where.append('''EXISTS (SELECT 1 FROM patient_record pr
            WHERE pr.person_id = sc.person_id AND pr.organization_id = %s)''')
        params.append(mapping.organization_id)
    cursor.execute(f'''
        SELECT {_COLUMNS}, count(*) OVER (), count(*) FILTER (WHERE h.has_order) OVER ()
        FROM patient_source_code sc {_RESOURCE}
        CROSS JOIN LATERAL (SELECT {_HAS_ORDER} AS has_order) h
        WHERE {' AND '.join(where)}
        ORDER BY h.has_order DESC, sc.person_id, sc.id
        LIMIT %s''', [*params, MAX_RECORDS])  # nosec B608
    rows = cursor.fetchall()
    if not rows:
        return [], 0, 0
    return [_Record(row[:-2]) for row in rows], rows[0][-2], rows[0][-1]


def _collect(cursor, mapping, empty):
    targets, total, with_order = _targets(cursor, mapping)
    summary = {**empty, 'records': total, 'records_read': len(targets), 'records_with_order': with_order}
    if not targets:
        return summary, {}

    by_person = defaultdict(list)
    for target in targets:
        by_person[target.person_id].append(target)

    # Reports that join results: the opened record itself when the code is a
    # report's, and a report that lists an opened record which names no order.
    # Records that do name one are not looked up: the lookup reads every
    # report the patient has.
    reports = defaultdict(dict)
    for target in targets:
        if target.resource_type == 'DiagnosticReport':
            reports[target.person_id][target.pk] = target
    unlinked = defaultdict(set)
    for target in targets:
        if not target.orders and target.resource_id and target.resource_type != 'DiagnosticReport':
            unlinked[target.person_id].add(f'{target.resource_type or "Observation"}/{target.resource_id}')
    if unlinked:
        for report in _walk(cursor, 'sc.omop_table <> ALL(%s)',
                            "r.res->>'resourceType' = 'DiagnosticReport' AND r.res->'result' @> ANY(o.refs)",
                            unlinked):
            reports[report.person_id][report.pk] = report

    def joins(report, target):
        return report.pk == target.pk or bool(target.resource_id and target.resource_id in report.results)

    ordered, listed = defaultdict(set), defaultdict(set)
    for target in targets:
        if target.orders:
            ordered[target.person_id] |= set(target.orders)
    for person_id, held in reports.items():
        for report in held.values():
            if any(joins(report, target) for target in by_person[person_id]):
                listed[person_id] |= report.results
    others = {}
    if ordered:
        others.update((record.pk, record) for record in _walk(
            cursor, 'sc.omop_table = ANY(%s)', "r.res->'basedOn' @> ANY(o.refs)", ordered))
    if listed:
        others.update((record.pk, record) for record in _walk(
            cursor, 'sc.omop_table = ANY(%s)', "r.res->>'id' = ANY(o.vals)", listed))
    others_by_person = defaultdict(list)
    for other in others.values():
        others_by_person[other.person_id].append(other)

    orders = set()            # (person, order key)
    names = Counter()         # order name -> records of the opened code
    found = defaultdict(lambda: {
        'orders': set(), 'units': Counter(), 'descriptions': Counter(),
        'loincs': Counter(), 'tables': Counter(),
    })
    linked_without_reference = 0
    for person_id, person_targets in by_person.items():
        candidates = others_by_person.get(person_id, [])
        for target in person_targets:
            # order key -> the names it goes by
            target_orders = {('order', reference): {display} - {''}
                             for reference, display in target.orders.items()}
            report_results = defaultdict(set)    # order key -> resource ids in it
            for report in reports.get(person_id, {}).values():
                if not joins(report, target):
                    continue
                shared = [reference for reference in report.orders if reference in target.orders]
                key = ('order', shared[0]) if shared else ('report', report.resource_id or report.pk)
                target_orders.setdefault(key, set())
                if report.description():
                    target_orders[key].add(report.description())
                report_results[key] |= report.results
            if not target_orders:
                continue
            if not target.orders:
                linked_without_reference += 1
            for key, labels in target_orders.items():
                orders.add((person_id, key))
                for label in labels:
                    names[label] += 1

            results = defaultdict(list)
            for candidate in candidates:
                if candidate.resource_type == 'DiagnosticReport' or _same_result(candidate, target):
                    continue
                shared = {key for key in target_orders
                          if (key[0] == 'order' and key[1] in candidate.orders)
                          or (candidate.resource_id and candidate.resource_id in report_results.get(key, ()))}
                if shared:
                    results[_result_key(candidate)].append((candidate, shared))
            for group in results.values():
                record = _primary([candidate for candidate, _ in group], target.vocabulary)
                entry = found[(record.vocabulary, record.code)]
                for _, shared in group:
                    entry['orders'] |= {(person_id, key) for key in shared}
                entry['tables'][record.omop_table] += 1
                if record.unit:
                    entry['units'][record.unit] += 1
                if record.description():
                    entry['descriptions'][record.description()] += 1
                # Another coding of the same Observation may be the LOINC.
                loinc = record.hospital_loinc() or next(
                    (c.hospital_loinc() for c, _ in group if c.hospital_loinc()), '')
                if loinc:
                    entry['loincs'][loinc] += 1

    summary['records_with_order'] = with_order + linked_without_reference
    summary['orders'] = len(orders)
    summary['order_names_total'] = len(names)
    summary['order_names'] = [
        {'name': name, 'count': count}
        for name, count in sorted(names.items(), key=lambda item: (-item[1], item[0]))[:MAX_ORDER_NAMES]
    ]
    return summary, found


def _mappings(mapping, found):
    """The current mapping of each co-occurring code: this hospital's when the
    opened mapping is a hospital's, else the global one, in the table the
    records sit in when the code is filed under several."""
    from omop_core.models import SourceCodeConceptMapping

    chosen = {}
    rows = SourceCodeConceptMapping.objects.filter(
        source_code__in={code for _, code in found},
    ).select_related('target_concept').order_by('id')
    for row in rows:
        key = (canonical_source_vocabulary(row.source_vocabulary_id), row.source_code)
        if key not in found or row.organization_id not in (None, mapping.organization_id):
            continue
        table = found[key]['tables'].most_common(1)[0][0]
        rank = (row.organization_id is None, row.omop_table != table, row.pk)
        if key not in chosen or rank < chosen[key][0]:
            chosen[key] = (rank, row)
    return {key: row for key, (_, row) in chosen.items()}


def _status(row):
    if row is None:
        return 'unmapped'
    if row.status == 'rejected':
        return 'rejected'
    # A queue row with no destination has not been mapped to anything.
    return row.status if row.target_concept_id else 'unmapped'


def _serialize(mapping, found):
    mappings = _mappings(mapping, found)
    destinations = {}
    for key, row in mappings.items():
        concept = row.target_concept
        if concept is not None and row.status != 'rejected' and concept.vocabulary_id == 'LOINC':
            destinations[key] = concept.concept_code
    hospital = {key: entry['loincs'].most_common(1)[0][0] for key, entry in found.items() if entry['loincs']}
    axes = loinc_axes.axes_for_codes({*destinations.values(), *hospital.values()})

    codes = []
    for key, entry in found.items():
        vocabulary, code = key
        row = mappings.get(key)
        # The mapping decides when it lands on a LOINC we hold the axes of;
        # otherwise the LOINC the hospital sent is the only word on it.
        basis, loinc = '', ''
        if destinations.get(key) in axes:
            basis, loinc = 'mapping', destinations[key]
        elif hospital.get(key) in axes:
            basis, loinc = 'hospital', hospital[key]
        described = (row.source_code_description if row else '').strip()
        if not described or described == code:
            described = entry['descriptions'].most_common(1)[0][0] if entry['descriptions'] else ''
        codes.append({
            'source_code': code,
            'source_vocabulary_id': vocabulary,
            'omop_table': row.omop_table if row else entry['tables'].most_common(1)[0][0],
            'description': described,
            'units': [unit for unit, _ in entry['units'].most_common(3)],
            'system': axes[loinc]['system'] if loinc else '',
            'method': axes[loinc]['method_type'] if loinc else '',
            'axes_basis': basis,
            'axes_loinc': loinc,
            'mapping_id': row.pk if row else None,
            'mapping_status': _status(row),
            'orders': len(entry['orders']),
        })
    codes.sort(key=lambda c: (-c['orders'], c['source_code'], c['source_vocabulary_id']))
    return codes


def co_occurring(mapping):
    """The coverage and co-occurring codes for *mapping*'s source code.

    ``timed_out`` is true when the inventory walk hit its time limit; the
    counts are then unknown, not zero.
    """
    empty = {'records': 0, 'records_read': 0, 'records_with_order': 0, 'orders': 0,
             'order_names': [], 'order_names_total': 0, 'codes': [], 'codes_total': 0}
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            previous = cursor.fetchone()[0]
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(STATEMENT_TIMEOUT_MS)])
            summary, found = _collect(cursor, mapping, empty)
            # Restore the caller's setting when this is nested in a request's
            # transaction. On failure the savepoint rollback restores it.
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
    except OperationalError:
        return {**empty, 'timed_out': True}
    # Most orders first; only the codes returned are looked up.
    kept = dict(sorted(found.items(), key=lambda item: (-len(item[1]['orders']), item[0][1], item[0][0]))[:MAX_CODES])
    summary.update(timed_out=False, codes=_serialize(mapping, kept), codes_total=len(found))
    return summary
