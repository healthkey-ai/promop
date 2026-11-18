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
  reports, because that lookup reads every report the patient has. The
  report is found by the relative reference the ETL stores
  (``Observation/<id>``); one that lists its results by absolute URL is not.
* One Observation is one result however many codings it carries. The codings
  of the record the dialog is open on are left out, and a co-occurring
  Observation is listed once, under the coding in the opened code's own code
  system when it has one.
"""
import json
import logging
from collections import Counter, defaultdict
from time import monotonic

from django.db import OperationalError, connection, transaction
from psycopg.errors import QueryCanceled

from omop_core.mapping import loinc_axes
from omop_core.services.source_vocabularies import (
    VOCABULARY_OID_ALIASES, canonical_source_vocabulary, fhir_source_vocabulary,
)

# Inventory records of the opened code that are walked. Every one costs a walk
# of that patient's inventory -- about 4,000 rows of stored resources each on
# HealthTree, near 20 ms a patient -- so this bounds the walks. Counting the
# records still reads one row per patient holding the code.
MAX_RECORDS = 25
MAX_CODES = 50
MAX_ORDER_NAMES = 8
# For the whole walk, not each statement: a walk that outruns it is reported,
# not waited for, and never holds a web worker near its own timeout. About a
# second when the patients' rows are cached; the first read of a code after
# they have left the cache can take several, and what it read stays cached
# for the retry the dialog offers.
BUDGET_MS = 8000
# Where a result is filed. Reports, orders and medications are half of an
# inventory and sit under other tables: results are looked for in these two
# tables only, and reports only outside them, so neither walk reads it all.
RESULT_TABLES = ('measurement', 'observation')

_RESOURCE = '''CROSS JOIN LATERAL (SELECT CASE
        WHEN jsonb_typeof(sc.source_metadata->'fhir') = 'object'
        THEN sc.source_metadata->'fhir' ELSE sc.source_metadata END AS res) r'''
# What `_Record.orders` accepts, so the count over every record and the
# records read agree: a list holding a reference that is a non-blank string.
# The array test is explicit because jsonpath's lax mode would wrap an object.
_HAS_ORDER = '''(jsonb_typeof(r.res->'basedOn') = 'array' AND jsonb_path_exists(r.res,
        '$.basedOn[*].reference ? (@.type() == "string" && @ like_regex "[^[:space:]]")'))'''
logger = logging.getLogger(__name__)

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
        # (system, code, display) of every coding that has a code, in the
        # resource's order. A system or display that is not text is blank.
        self.codings = []
        for coding in code['coding'] if isinstance(code.get('coding'), list) else []:
            if isinstance(coding, dict) and isinstance(coding.get('code'), (str, int)) \
                    and str(coding['code']).strip():
                self.codings.append(tuple(
                    str(coding['code']).strip() if key == 'code'
                    else coding[key].strip() if isinstance(coding.get(key), str) else ''
                    for key in ('system', 'code', 'display')))
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
        return {code for _, code, _ in self.codings}

    def system_of(self, wanted):
        """The system the resource gives *wanted*, when it lists that code."""
        return next((system for system, code, _ in self.codings if code == wanted), '')

    def described(self, wanted):
        """What the source calls *wanted*: the resource's text, which names the
        test where a shared display does not (#1778), else that coding's display."""
        return self.text or next(
            (display for _, code, display in self.codings if code == wanted and display), '')

    def loincs(self):
        """The LOINC codes the hospital sent on this record, its own first."""
        own = [self.code] if loinc_axes.is_loinc(self.vocabulary) else []
        return own + [code for system, code, _ in self.codings
                      if loinc_axes.is_loinc(system) and code not in own]


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


def _is_opened_result(records, target):
    """Whether the Observation these rows sampled is the opened record's own.

    Resource ids decide when both sides carry one. Without them, an
    Observation that lists the opened code is the same test; one that carries
    no coding at all is, when the opened record lists its row's code.
    """
    record = records[0]
    if record.resource_id and target.resource_id:
        return record.resource_id == target.resource_id
    if record.codings:
        return target.code in record.coding_codes
    return any(r.code in target.coding_codes for r in records)


def _result_key(record):
    """What makes two inventory rows samples of one Observation."""
    if record.resource_id:
        return ('id', record.resource_id)
    return ('codings', frozenset((system, code) for system, code, _ in record.codings)) \
        if record.codings else ('row', record.pk)


def _listed_coding(record, system):
    """The coding an Observation is listed under: the one in the opened code's
    own code system, else a local code before the LOINC beside it (which the
    table shows as the hospital's), else the first. None when the resource
    carries no coding."""
    codings = record.codings
    return next((c for c in codings if system and c[0] == system), None) \
        or next((c for c in codings if not loinc_axes.is_loinc(c[0])), None) \
        or (codings[0] if codings else None)


def _vocabulary_of(coding, target, system, held):
    """``(vocabulary, unit)`` of *coding* in this patient's inventory.

    *held* are the patient's inventory rows for that code, as ``(vocabulary,
    unit)``. The inventory says how the ETL named the code system; the
    resource's own system only picks between rows, or stands in for a code
    the inventory does not hold.
    """
    wanted = target.vocabulary if system and coding[0] == system else canonical_source_vocabulary(
        fhir_source_vocabulary(coding[0]) or coding[0])
    if not held:
        return wanted, ''
    return min(held, key=lambda row: (row[0] != wanted, loinc_axes.is_loinc(row[0]) != loinc_axes.is_loinc(coding[0]),
                                      row[0]))


class _Budget:
    """A cursor whose statements share one time budget.

    Before each statement the limit is set to what is left, so the walk as a
    whole is bounded; a statement that outruns it is cancelled by the server.
    """

    def __init__(self, cursor, budget_ms):
        self._cursor = cursor
        self._deadline = monotonic() + budget_ms / 1000

    def execute(self, sql, params=None):
        remaining = int((self._deadline - monotonic()) * 1000)
        if remaining <= 0:
            raise TimeoutError('The co-occurring walk exceeded its time budget.')
        self._cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(remaining)])
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return self._cursor.fetchall()


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
    # The ETL posts the system it received; the mapping stores the canonical id.
    wanted = canonical_source_vocabulary(mapping.source_vocabulary_id)
    vocabularies = sorted({mapping.source_vocabulary_id, wanted, *(
        alias for alias, canonical in VOCABULARY_OID_ALIASES.items() if canonical == wanted)})
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
        # Only an Observation is a report's result.
        if not target.orders and target.resource_id and target.resource_type in ('Observation', ''):
            unlinked[target.person_id].add(f'Observation/{target.resource_id}')
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

    order_names = {}          # (person, order key) -> the names it goes by
    in_report = 0             # records read that name no order and a report joins
    grouped = []              # (person, target, rows that sampled one Observation, order keys shared)
    for person_id, person_targets in by_person.items():
        candidates = others_by_person.get(person_id, [])
        for target in person_targets:
            target_orders = {('order', reference): {display} - {''}
                             for reference, display in target.orders.items()}
            report_results = defaultdict(set)    # order key -> resource ids in it
            for report in reports.get(person_id, {}).values():
                if not joins(report, target):
                    continue
                shared = [reference for reference in report.orders if reference in target.orders]
                key = ('order', shared[0]) if shared else ('report', report.resource_id or report.pk)
                # The report names an order its reference does not.
                named = report.described(report.code)
                if named and not target_orders.get(key):
                    target_orders[key] = {named}
                target_orders.setdefault(key, set())
                report_results[key] |= report.results
            if not target_orders:
                continue
            if not target.orders:
                in_report += 1
            for key, labels in target_orders.items():
                order_names.setdefault((person_id, key), set()).update(labels)

            results = defaultdict(lambda: ([], set()))
            for candidate in candidates:
                if candidate.resource_type == 'DiagnosticReport':
                    continue
                shared = {key for key in target_orders
                          if (key[0] == 'order' and key[1] in candidate.orders)
                          or (candidate.resource_id and candidate.resource_id in report_results.get(key, ()))}
                if shared:
                    rows, keys = results[_result_key(candidate)]
                    rows.append(candidate)
                    keys |= shared
            grouped += [(person_id, target, rows, keys) for rows, keys in results.values()
                        if not _is_opened_result(rows, target)]

    # Each Observation is listed under one of its own codings, which need not
    # be the code of the row that happened to sample it: a LOINC shared by
    # several local codes samples only one of them. The patient's inventory
    # row for the listed code says which code system the ETL filed it under.
    listed = [(_listed_coding(rows[0], target.system_of(target.code)), person_id, target, rows, keys)
              for person_id, target, rows, keys in grouped]
    unknown = sorted({(person_id, coding[1]) for coding, person_id, _, rows, _ in listed
                      if coding and not any(row.code == coding[1] for row in rows)})
    held = defaultdict(list)
    for coding, person_id, _, rows, _ in listed:
        for row in rows:
            held[(person_id, row.code)].append((row.vocabulary, row.unit))
    if unknown:
        cursor.execute('''
            SELECT sc.person_id, sc.source_value, sc.source_vocabulary_id,
                   COALESCE(sc.source_unit_override, sc.source_unit)
            FROM unnest(%s::int[], %s::text[]) AS o(person_id, code)
            JOIN patient_source_code sc ON sc.person_id = o.person_id AND sc.source_value = o.code''',
                       [[p for p, _ in unknown], [c for _, c in unknown]])
        for person_id, code, vocabulary, unit in cursor.fetchall():
            held[(person_id, code)].append((canonical_source_vocabulary(vocabulary), (unit or '').strip()))

    found = defaultdict(lambda: {
        'orders': set(), 'units': Counter(), 'descriptions': Counter(), 'loincs': Counter(),
    })
    for coding, person_id, target, rows, keys in listed:
        record = rows[0]
        if coding is None:
            # A resource with no coding: all there is to list is the row's own code.
            row = min(rows, key=lambda r: (r.vocabulary != target.vocabulary, r.vocabulary, r.code))
            code, vocabulary, unit = row.code, row.vocabulary, row.unit
        else:
            code = coding[1]
            vocabulary, unit = _vocabulary_of(coding, target, target.system_of(target.code),
                                              held.get((person_id, code)))
        if (vocabulary, code) == (target.vocabulary, target.code):
            continue    # the opened test again, resulted twice in the order
        entry = found[(vocabulary, code)]
        entry['orders'] |= {(person_id, key) for key in keys}
        if unit:
            entry['units'][unit] += 1
        if record.described(code):
            entry['descriptions'][record.described(code)] += 1
        # A LOINC the opened record carries too says nothing about this one:
        # Epic files every vital sign under 8716-3 as well.
        shared_loincs = set(target.loincs())
        loinc = code if loinc_axes.is_loinc(vocabulary) else next(
            (c for row in rows for c in row.loincs() if c not in shared_loincs), '')
        if loinc:
            entry['loincs'][loinc] += 1

    names = Counter(label for labels in order_names.values() for label in labels)
    summary['records_in_report'] = in_report
    summary['orders'] = len(order_names)
    summary['order_names_total'] = len(names)
    summary['order_names'] = [
        {'name': name, 'count': count}
        for name, count in sorted(names.items(), key=lambda item: (-item[1], item[0]))[:MAX_ORDER_NAMES]
    ]
    return summary, found


def _mappings(mapping, found):
    """The current mapping of each co-occurring code. A hospital's opened
    mapping sees that hospital's row before the global one and never another
    hospital's. A global one sees the global row first, else a hospital's:
    its walk read every hospital's patients, and the code is in the queue."""
    from omop_core.models import SourceCodeConceptMapping

    chosen = {}
    rows = SourceCodeConceptMapping.objects.filter(
        source_code__in={code for _, code in found},
    ).select_related('target_concept').order_by('id')
    for row in rows:
        key = (canonical_source_vocabulary(row.source_vocabulary_id), row.source_code)
        if key not in found:
            continue
        if mapping.organization_id and row.organization_id not in (None, mapping.organization_id):
            continue
        rank = (row.organization_id != mapping.organization_id, row.organization_id is not None, row.pk)
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
    """One row per code, in the order *found* is in."""
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
    return codes


def co_occurring(mapping):
    """The coverage and co-occurring codes for *mapping*'s source code.

    ``applies`` is false for a code filed under a table that holds no results
    (conditions, drugs, procedures). ``timed_out`` is true when the inventory walk hit its time
    limit; the counts are then unknown, not zero.
    """
    empty = {'applies': True, 'timed_out': False, 'records': 0, 'records_read': 0,
             'records_with_order': 0, 'records_in_report': 0, 'orders': 0,
             'order_names': [], 'order_names_total': 0, 'codes': [], 'codes_total': 0}
    # A condition, a drug or a procedure is not resulted in a panel: nothing
    # is read for one, and the dialog shows no section.
    if (mapping.omop_table or '') not in (*RESULT_TABLES, ''):
        return {**empty, 'applies': False}
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            previous = cursor.fetchone()[0]
            summary, found = _collect(_Budget(cursor, BUDGET_MS), mapping, empty)
            # Restore the caller's setting when this is nested in a request's
            # transaction. On failure the savepoint rollback restores it.
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous])
    except (OperationalError, TimeoutError) as exc:
        # Only the time limit is an answer. A dropped connection or any other
        # database fault is an error, and is not reported as a slow walk.
        if isinstance(exc, OperationalError) and not isinstance(exc.__cause__, QueryCanceled):
            raise
        # The mapping id only: no code text or order name belongs in a log.
        logger.warning('Co-occurring walk for mapping %s exceeded %s ms', mapping.pk, BUDGET_MS)
        return {**empty, 'timed_out': True}
    # Most orders first; only the codes returned are looked up.
    kept = dict(sorted(found.items(), key=lambda item: (-len(item[1]['orders']), item[0][1], item[0][0]))[:MAX_CODES])
    summary.update(codes=_serialize(mapping, kept), codes_total=len(found))
    return summary
