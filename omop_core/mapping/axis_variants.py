"""One source code, more than one LOINC: axis variants.

A hospital lab code is one code to the sender, but its results can arrive in
units that imply different LOINC axes -- glucose in mg/dL is a mass
concentration (``MCnc``) and in mmol/L a substance concentration (``SCnc``),
and LOINC gives those two different codes. Mapping the hospital code to
either one mislabels every record in the other unit.

So the units a code has been seen with are grouped by the axis values they
imply. Two or more groups are *variants*: each gets its own destination, and
ingest sends a record to the variant its unit belongs to (:func:`variant_for`).

The evidence is the same unit evidence the approval unit check reads --
stored Measurement rows plus the imported source-code inventory -- and the
per-patient source codes. A unit that implies no single property (``%`` is
mass, number or volume fraction) is a variant of its own, keyed by the unit,
because nothing says which group it belongs to.

Units are the discriminator because they are the one per-record value every
ingest path carries. A specimen that varies across patients is reported as a
*non-routable* variation: the curator sees it, but ingest has nothing to route
on.
"""
from collections import Counter, defaultdict

from django.db import transaction
from django.utils import timezone

from omop_core.mapping import axis_evidence, loinc_axes
from omop_core.services.canonical_units import unit_code

#: Codes with this many variants or more are flagged in the queue.
FLAG_THRESHOLD = 2
# Keys per transaction in refresh_all.
REFRESH_BATCH = 500

unit_property = loinc_axes.unit_property


def observed_units(mapping, unit_counts=None):
    """Every normalized unit the code's records arrived in, commonest first."""
    if not mapping:
        return []
    counts = unit_counts if unit_counts is not None else _unit_counts(mapping)
    return [unit for unit, _ in counts.most_common()]


def unit_counts(mapping):
    """The unit evidence :func:`detect` and :func:`observed_units` read, once."""
    return _unit_counts(mapping)


def axis_key(values):
    """The canonical form of axis values; a set reads ``property=ACnc,CCnc``."""
    def text(value):
        return ','.join(str(v) for v in value) if isinstance(value, (list, tuple)) else value
    return '|'.join(f'{axis}={text(values[axis])}' for axis in loinc_axes.AXES if axis in values)


def _unit_counts(mapping):
    """``Counter`` of normalized units for one mapping, from every evidence source."""
    return unit_counts_for([mapping]).get(mapping.pk, Counter())


#: ``source`` of an evidence entry this instance derived from its own patient
#: data (#2043). A seed load leaves such evidence alone.
PATIENT_DATA_SOURCE = 'patient-data'


def unit_counts_for(mappings, include_imported=True):
    """``{mapping_id: Counter}`` of normalized units, in a fixed number of queries.

    Per-patient source codes (keyed by ``(source_vocabulary_id, source_code)``)
    are the primary evidence: one query for the batch.  Imported inventory
    evidence (``source_unit_evidence``) is unioned unless ``include_imported``
    is False. ``include_imported=False`` reads only what this instance's
    patients say, which is what a rebuild of the stored evidence writes.

    The Measurement aggregate (``mapping_unit_consistency._unit_counts``) is
    deliberately excluded: it groups only on ``measurement_source_value``, so
    every hospital code sharing the same test name gets the same inflated
    global count. PSC rows give the correct per-code counts.
    """
    from django.db.models import Q

    from omop_core.models import PatientSourceCode
    from omop_core.services.mapping_unit_consistency import _imported_unit_counts

    mappings = [m for m in mappings if m is not None]
    if not mappings:
        return {}
    measured = [m for m in mappings if m.omop_table == 'measurement']
    imported = _imported_unit_counts(measured) if measured and include_imported else {}
    result = {m.pk: imported.get(m.pk, Counter()) for m in mappings}

    match = Q()
    for m in mappings:
        match |= Q(source_value=m.source_code, source_vocabulary_id=m.source_vocabulary_id,
                   **({'omop_table': m.omop_table} if m.omop_table else {}))
    by_key = defaultdict(list)
    for m in mappings:
        by_key[(m.source_vocabulary_id, m.source_code)].append(m)
    patient = defaultdict(Counter)
    for vocab, code, table, unit, override, count in PatientSourceCode.objects.filter(match).values_list(
            'source_vocabulary_id', 'source_value', 'omop_table', 'source_unit',
            'source_unit_override', 'occurrence_count'):
        normalized = unit_code(override if override is not None else unit)
        if not normalized:
            continue
        for m in by_key.get((vocab, code), ()):
            if not m.omop_table or m.omop_table == table:
                patient[m.pk][normalized] += max(1, count or 0)
    return {pk: counts | patient.get(pk, Counter()) for pk, counts in result.items()}


def _is_imported(entry):
    """An evidence entry a seed or inventory wrote: it names its ``source``. A
    text term's unit (``text_terms.record``) names none and is this instance's own."""
    return bool(entry.get('source')) and entry.get('source') != PATIENT_DATA_SOURCE


def patient_unit_evidence(mappings):
    """``({mapping_id: entries}, {mapping_id: Counter})`` from this instance's
    own patient data (#2043).

    The units come from the per-patient source codes recorded on each loaded
    patient; seeded evidence is not read. One entry per normalized unit,
    largest first: ``count`` records, ``patients`` distinct people,
    ``source`` = ``patient-data``. The Counter is what the entries say, for the
    caller to refresh variants with.

    The Measurement aggregate (``_unit_counts``) is deliberately excluded: it
    groups only on ``measurement_source_value``, not on
    ``(source_vocabulary_id, source_code)``, so every hospital code that shares
    the same test name gets the same inflated global count. PSC rows are keyed
    per hospital code and give the correct per-code evidence.
    """
    from django.db.models import Q, Sum

    from omop_core.models import PatientSourceCode

    mappings = [m for m in mappings if m is not None]
    if not mappings:
        return {}, {}
    match = Q()
    for m in mappings:
        match |= Q(source_value=m.source_code, source_vocabulary_id=m.source_vocabulary_id,
                   **({'omop_table': m.omop_table} if m.omop_table else {}))
    by_key = defaultdict(list)
    for m in mappings:
        by_key[(m.source_vocabulary_id, m.source_code)].append(m)
    recorded = defaultdict(Counter)
    people = defaultdict(lambda: defaultdict(set))
    rows = (PatientSourceCode.objects.filter(match)
            .values_list('source_vocabulary_id', 'source_value', 'omop_table', 'source_unit',
                         'source_unit_override', 'person_id')
            .annotate(records=Sum('occurrence_count')))
    for vocab, code, table, unit, override, person_id, records in rows:
        # Spellings that normalize to one unit are one unit, and a person seen
        # in two spellings is one person.
        normalized = unit_code(override if override is not None else unit)
        if not normalized:
            continue
        for m in by_key.get((vocab, code), ()):
            if not m.omop_table or m.omop_table == table:
                recorded[m.pk][normalized] += max(1, records or 0)
                people[m.pk][normalized].add(person_id)
    entries, counts = {}, {}
    for m in mappings:
        counts[m.pk] = recorded.get(m.pk, Counter())
        entries[m.pk] = []
        for unit, count in sorted(counts[m.pk].items(), key=lambda item: (-item[1], item[0])):
            entry = {'display': unit, 'code': unit, 'normalized': unit, 'count': count,
                     'source': PATIENT_DATA_SOURCE}
            if people[m.pk].get(unit):
                entry['patients'] = len(people[m.pk][unit])
            entries[m.pk].append(entry)
    return entries, counts


def rebuild_unit_evidence(mappings):
    """Store each mapping's unit evidence as its own patient data says (#2043).

    Replaces whatever a seed or inventory wrote, which on the seeded instances
    was the inventory's label-level counts repeated on every code with the
    label (#2041). Units this instance recorded itself with no ``source`` (a
    text term's lookups) are kept. A code with no patient data here gets no
    units. Returns ``(changed, counts)``: how many mappings changed, and each
    mapping's unit Counter as the stored evidence now reads, for the variant
    refresh that follows.
    """
    from omop_core.models import SourceCodeConceptMapping
    from omop_core.services.mapping_unit_consistency import _imported_unit_counts

    mappings = [m for m in mappings if m is not None]
    patient, counts = patient_unit_evidence(mappings)
    changed = []
    count_changed = []
    for m in mappings:
        kept = [e for e in (m.source_unit_evidence or []) if isinstance(e, dict) and not e.get('source')]
        rebuilt = patient.get(m.pk, []) + kept
        if (m.source_unit_evidence or []) != rebuilt:
            m.source_unit_evidence = rebuilt
            changed.append(m)
        if kept:
            counts[m.pk] = counts.get(m.pk, Counter()) | _imported_unit_counts([m]).get(m.pk, Counter())
        # Update occurrence_count from the PSC totals (#2110). The seeded
        # inventory wrote label-level counts on every code with the label;
        # the per-patient source codes give the correct per-code count.
        psc_total = sum(counts.get(m.pk, Counter()).values())
        if psc_total and m.occurrence_count != psc_total:
            m.occurrence_count = psc_total
            count_changed.append(m)
    if changed:
        SourceCodeConceptMapping.objects.bulk_update(changed, ['source_unit_evidence'], batch_size=500)
    if count_changed:
        SourceCodeConceptMapping.objects.bulk_update(count_changed, ['occurrence_count'], batch_size=500)
    _store_fhir_codings(mappings)
    return len(changed), counts


def _patient_fhir_codings(mapping):
    """LOINC codings the per-patient metadata carries for this code.

    Returns a list of ``{'system': ..., 'code': ..., 'display': ...}`` dicts,
    deduplicated by code and ordered by total occurrence count (most common
    first).  Only LOINC codings are collected: they are the ones
    ``loinc_axes.infer()`` uses to settle axes.
    """
    from omop_core.models import PatientSourceCode

    seen = {}  # code → {coding dict, total count}
    rows = PatientSourceCode.objects.filter(
        source_value=mapping.source_code,
        source_vocabulary_id=mapping.source_vocabulary_id,
    ).exclude(source_metadata={})
    for metadata, count in rows.order_by('-occurrence_count').values_list('source_metadata', 'occurrence_count')[:2000]:
        resource = loinc_axes._resource(metadata)
        for coding in loinc_axes._codings(resource):
            if loinc_axes.is_loinc(coding.get('system')) and coding.get('code'):
                code = coding['code']
                if code not in seen:
                    seen[code] = {
                        'coding': {k: coding[k] for k in ('system', 'code', 'display')
                                   if k in coding},
                        'count': 0,
                    }
                seen[code]['count'] += max(1, count or 0)
    return [entry['coding'] for entry in
            sorted(seen.values(), key=lambda e: -e['count'])]


def _store_fhir_codings(mappings):
    """Copy LOINC codings from per-patient metadata onto the mapping row.

    Stored under ``source_metadata['fhir']['code']['coding']`` so
    ``loinc_axes._resource()`` → ``_codings()`` finds them naturally.
    Only writes mappings whose metadata is missing the codings.
    """
    from omop_core.models import SourceCodeConceptMapping

    updated = []
    for m in mappings:
        meta = m.source_metadata or {}
        # Already has fhir codings — a previous walk or a direct FHIR ingest.
        if isinstance(meta.get('fhir'), dict) and meta['fhir'].get('code', {}).get('coding'):
            continue
        codings = _patient_fhir_codings(m)
        if not codings:
            continue
        meta = dict(meta)  # shallow copy
        existing_fhir = dict(meta.get('fhir') or {})
        existing_fhir['code'] = {'coding': codings}
        meta['fhir'] = existing_fhir
        m.source_metadata = meta
        updated.append(m)
    if updated:
        SourceCodeConceptMapping.objects.bulk_update(updated, ['source_metadata'], batch_size=500)
    return len(updated)


def is_patient_evidence(evidence):
    """Whether stored evidence is this instance's own (:func:`rebuild_unit_evidence`),
    which a seed or inventory load must not overwrite."""
    return any(isinstance(entry, dict) and entry.get('source') == PATIENT_DATA_SOURCE
               for entry in evidence or [])


def evidence_keys():
    """Every ``(source_vocabulary_id, source_code)`` a rebuild rewrites: mapped
    codes with stored evidence, mapped codes on loaded patients' records with
    a unit, and codes already flagged. A patient code with no mapping has
    nothing to rewrite."""
    from django.db.models import Exists, OuterRef, Q

    from omop_core.models import PatientSourceCode, SourceCodeConceptMapping

    keys = set(SourceCodeConceptMapping.objects.exclude(source_unit_evidence=[])
               .values_list('source_vocabulary_id', 'source_code'))
    keys |= set(SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0)
                .values_list('source_vocabulary_id', 'source_code'))
    with_units = PatientSourceCode.objects.filter(
        Q(source_unit__gt='') | Q(source_unit_override__gt=''),
        source_vocabulary_id=OuterRef('source_vocabulary_id'), source_value=OuterRef('source_code'))
    keys |= set(SourceCodeConceptMapping.objects.filter(Exists(with_units))
                .values_list('source_vocabulary_id', 'source_code'))
    return keys


def _specimen_systems(mapping):
    """``Counter`` of LOINC systems the per-patient metadata names."""
    from omop_core.models import PatientSourceCode

    systems = Counter()
    rows = PatientSourceCode.objects.filter(
        source_value=mapping.source_code,
        source_vocabulary_id=mapping.source_vocabulary_id,
    ).exclude(source_metadata={})
    for metadata, count in rows.values_list('source_metadata', 'occurrence_count')[:2000]:
        system, _specimen = loinc_axes.specimen_system(loinc_axes._resource(metadata))
        if system:
            systems[system] += max(1, count or 0)
    return systems


def detect(mapping, unit_counts=None, notes=True):
    """``(variants, notes)`` for one mapping.

    ``variants`` is a list of ``{axis_key, axis_values, units, occurrence_count}``
    ordered by occurrence, largest first. A code whose units all imply the same
    axes has one variant -- which is not a variant at all, and is what most
    codes look like. ``notes`` describes variation that cannot be routed.
    """
    groups = defaultdict(lambda: {'units': Counter(), 'axis_values': {}})
    counts = unit_counts if unit_counts is not None else _unit_counts(mapping)
    unrecognised = Counter()
    for unit, count in counts.items():
        if not loinc_axes.unit_is_recognised(unit):
            # A misspelling or unlisted unit says nothing about the axes; a
            # variant of its own would flag one test as two.
            unrecognised[unit] += count
            continue
        options = loinc_axes.unit_property_options(unit)
        if options:
            # One property, or the set a unit such as % or U/L fits (#2029):
            # U/L and IU/L fit the same set and are one variant.
            values = {'property': loinc_axes._property_value(options), 'scale_type': 'Qn'}
            key = axis_key(values)
        else:
            # A unit that fits no property is still a quantity, and nothing
            # more, so it is a variant of its own, keyed by unit.
            values = {'scale_type': 'Qn'}
            key = f'unit={unit}'
        groups[key]['units'][unit] += count
        groups[key]['axis_values'] = values
    variants = [
        {
            'axis_key': key,
            'axis_values': group['axis_values'],
            'units': [unit for unit, _ in group['units'].most_common()],
            'occurrence_count': sum(group['units'].values()),
        }
        for key, group in groups.items()
    ]
    variants.sort(key=lambda v: (-v['occurrence_count'], v['axis_key']))

    if not notes:
        return variants, []
    notes = []
    if unrecognised:
        notes.append('Units not recognised, so not grouped: '
                     + ', '.join(f'{unit} ×{count}' for unit, count in unrecognised.most_common(8)) + '.')
    systems = _specimen_systems(mapping)
    if len(systems) >= 2:
        notes.append(
            'Specimens differ across patients ('
            + ', '.join(f'{system} ×{count}' for system, count in systems.most_common())
            + '). Ingest cannot route on specimen, so these share a destination.'
        )
    return variants, notes


def _legacy_row(existing, variant):
    """A row stored under the per-unit key an ambiguous unit had before its
    property set was derived (``unit=%``, #2029), re-keyed to ``variant``'s key
    so its destination is kept. Of several, the one with a decision wins; the
    rest, undecided, are deleted rather than kept as unseen duplicates."""
    legacy = [existing[key] for key in list(existing)
              if key.startswith('unit=') and key != variant['axis_key']
              and key[len('unit='):] in variant['units']]
    if not legacy:
        return None
    legacy.sort(key=lambda row: (row.status == 'approved', row.target_concept_id is not None), reverse=True)
    for row in legacy[1:]:
        if row.status != 'approved' and row.target_concept_id is None:
            existing.pop(row.axis_key)
            row.delete()
    row = existing.pop(legacy[0].axis_key)
    row.axis_key = variant['axis_key']
    return row


@transaction.atomic
def refresh(mapping, unit_counts=None):
    """Store the detected variants and the count the queue flags.

    Existing variants keep their destination and status: a re-detection must
    not undo a curator's choice. A variant no longer seen is kept when it
    carries a destination or an approval, because its units may simply not
    have arrived in the latest load; one with nothing decided is deleted.
    Returns the variants as stored.
    """
    from omop_core.models import SourceCodeAxisVariant

    variants, _notes = detect(mapping, unit_counts, notes=False)
    existing = {v.axis_key: v for v in mapping.axis_variants.all()}
    if len(variants) >= FLAG_THRESHOLD:
        for variant in variants:
            row = existing.get(variant['axis_key'])
            if row is None:
                row = _legacy_row(existing, variant)
            if row is None:
                row = SourceCodeAxisVariant(mapping=mapping, axis_key=variant['axis_key'])
            row.axis_values = variant['axis_values']
            row.units = variant['units']
            row.occurrence_count = variant['occurrence_count']
            row.save()
            existing[variant['axis_key']] = row
    # Update occurrence_count on surviving variant rows that match detected
    # variants, even when below FLAG_THRESHOLD (#2111). A rebuild can correct
    # inflated counts from pre-rebuild Measurement aggregation.
    if len(variants) < FLAG_THRESHOLD:
        detected_by_key = {v['axis_key']: v for v in variants}
        for key, row in existing.items():
            variant = detected_by_key.get(key)
            if variant and row.pk and row.occurrence_count != variant['occurrence_count']:
                row.occurrence_count = variant['occurrence_count']
                row.save(update_fields=['occurrence_count'])
    # A variant the evidence no longer shows keeps a destination or an
    # approval, which is a curator's work; one with nothing decided was only
    # ever evidence, and evidence that has gone is deleted (#2043).
    seen = {variant['axis_key'] for variant in variants}
    for key, row in list(existing.items()):
        if key not in seen and row.pk and row.status != 'approved' and row.target_concept_id is None:
            existing.pop(key)
            row.delete()
    # An approved variant routes ingest, so its count must not drop to 0 just
    # because the latest evidence lost its units -- routing is gated on it,
    # and the rows its approval re-pointed would disagree with new records.
    detected = len(variants) if len(variants) >= FLAG_THRESHOLD else 0
    approved = any(v.status == 'approved' for v in existing.values())
    count = max(detected, len(existing)) if approved else detected
    if mapping.axis_variant_count != count:
        mapping.axis_variant_count = count
        mapping.save(update_fields=['axis_variant_count'])
    return list(mapping.axis_variants.select_related('target_concept').order_by('-occurrence_count', 'axis_key'))


def refresh_codes(keys, rebuild_evidence=False, summary=None):
    """Refresh every mapping for ``(source_vocabulary_id, source_code)`` keys.

    Failures are logged, not raised: this runs after ingest writes, and a
    variant count is curation evidence, not a reason to fail an upload. With
    ``rebuild_evidence`` the batch's stored unit evidence is first rewritten
    from the instance's patient data (#2043). A ``summary``
    (``axis_evidence.Summary``) is given the axis evidence of every mapping
    refreshed (#2051).
    """
    import logging

    from django.db.models import Q

    from omop_core.models import SourceCodeConceptMapping

    keys = {(vocab or '', code) for vocab, code in keys if code}
    if not keys:
        return 0
    match = Q()
    for vocab, code in keys:
        match |= Q(source_vocabulary_id=vocab, source_code=code)
    refreshed = 0
    mappings = list(SourceCodeConceptMapping.objects.filter(match))
    if rebuild_evidence:
        # The rebuild has already read the evidence the refresh needs.
        _changed, counts = rebuild_unit_evidence(mappings)
    else:
        counts = unit_counts_for(mappings)
    resources = axis_evidence.resources_by_code(mappings) if summary is not None else {}
    for mapping in mappings:
        units = counts.get(mapping.pk, Counter())
        try:
            # A savepoint each, so one failed row cannot abort the rest.
            with transaction.atomic():
                refresh(mapping, units)
            refreshed += 1
        except Exception:  # noqa: BLE001 - evidence must not fail ingest
            logging.getLogger(__name__).warning(
                'Axis variant refresh failed for mapping %s.', mapping.pk, exc_info=True)
            continue
        if summary is None:
            continue
        try:
            # The count is reporting (#2051): it must not undo the refresh,
            # and a code it cannot read is left out of the summary.
            variants, _notes = detect(mapping, units, notes=False)
            summary.add(axis_evidence.code_evidence(
                mapping, list(units), resources.get(mapping.pk, []), variant_count=len(variants)))
        except Exception:  # noqa: BLE001 - reporting must not fail the walk
            logging.getLogger(__name__).warning(
                'Axis evidence count failed for mapping %s; it is left out of the summary.',
                mapping.pk, exc_info=True)
    return refreshed


def multi_unit_keys():
    """Every ``(source_vocabulary_id, source_code)`` that may have variants.

    The codes with two or more units in any evidence source -- imported unit
    evidence, per-patient source codes, stored Measurement rows -- plus every
    code already flagged, so a code that stopped varying is cleared.
    """
    from django.db.models import Count, F, Func, IntegerField

    from omop_core.models import Measurement, PatientSourceCode, SourceCodeConceptMapping

    class JsonLength(Func):
        function = 'jsonb_array_length'
        output_field = IntegerField()

    keys = set(
        SourceCodeConceptMapping.objects
        .annotate(units=JsonLength(F('source_unit_evidence')))
        .filter(units__gte=FLAG_THRESHOLD)
        .values_list('source_vocabulary_id', 'source_code')
    )
    keys |= set(
        SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0)
        .values_list('source_vocabulary_id', 'source_code')
    )
    keys |= set(
        PatientSourceCode.objects.exclude(source_unit='')
        .values('source_vocabulary_id', 'source_value')
        .annotate(units=Count('source_unit', distinct=True))
        .filter(units__gte=FLAG_THRESHOLD)
        .values_list('source_vocabulary_id', 'source_value')
    )
    # Stored rows carry no vocabulary, so their source values are matched
    # against measurement mappings by code.
    multi_unit_values = set(
        Measurement.objects.exclude(unit_source_value__isnull=True)
        .exclude(unit_source_value='')
        .values('measurement_source_value')
        .annotate(units=Count('unit_source_value', distinct=True))
        .filter(units__gte=FLAG_THRESHOLD)
        .values_list('measurement_source_value', flat=True)
    )
    if multi_unit_values:
        keys |= set(
            SourceCodeConceptMapping.objects
            .filter(omop_table='measurement', source_code__in=multi_unit_values)
            .values_list('source_vocabulary_id', 'source_code')
        )
    return keys


def refresh_all(keys=None, batch_size=REFRESH_BATCH, on_batch=None, rebuild_evidence=False, summary=None):
    """Refresh ``keys`` (default :func:`multi_unit_keys`, or :func:`evidence_keys`
    with ``rebuild_evidence``) a batch at a time.

    One transaction per batch: the whole backlog in one would hold its locks
    for minutes, and one OR over every key makes a statement the planner
    chokes on. ``on_batch(done, refreshed, last_key)`` is called after each
    batch, which is how the worker job records its progress and where to
    resume. Returns the number of mappings refreshed.
    """
    if keys is None:
        keys = evidence_keys() if rebuild_evidence else multi_unit_keys()
    keys = sorted(keys, key=key_text)
    refreshed = 0
    for start in range(0, len(keys), batch_size):
        batch = keys[start:start + batch_size]
        with transaction.atomic():
            refreshed += refresh_codes(batch, rebuild_evidence=rebuild_evidence, summary=summary)
        if on_batch is not None:
            on_batch(start + len(batch), refreshed, batch[-1])
    return refreshed


def key_text(key):
    """The sort key of a ``(vocabulary, code)``, also stored as a walk's cursor."""
    return f"{key[0] or ''}\x1f{key[1] or ''}"


def usable_destination(concept, mapping=None):
    """Whether ``concept`` may be a variant's destination: active, standard,
    and in the mapping's domain when it has one -- what the mapping-level
    resolver also requires of a destination."""
    if concept is None or concept.invalid_reason or concept.standard_concept != 'S':
        return False
    domain = getattr(mapping, 'domain_id', '') if mapping is not None else ''
    return not domain or concept.domain_id == domain


def variant_for(mapping, source_unit):
    """The approved variant a record in ``source_unit`` belongs to, or None.

    A variant whose concept has since been retired or made non-standard by a
    vocabulary load is skipped, so the record falls back to the mapping.
    """
    if mapping is None or not getattr(mapping, 'axis_variant_count', 0) or not source_unit:
        return None
    unit = unit_code(source_unit)
    prop = unit_property(unit)
    approved = [v for v in mapping.axis_variants.filter(status='approved', target_concept__isnull=False)
                                                  .select_related('target_concept')
                                                  .order_by('-occurrence_count', 'axis_key')
                if usable_destination(v.target_concept, mapping)]
    for variant in approved:
        if unit in (variant.units or []):
            return variant
    if prop:
        # The variant for exactly this property first; then one whose set
        # (U/L: catalytic or arbitrary) includes it.
        for exact in (True, False):
            for variant in approved:
                options = loinc_axes.options_of((variant.axis_values or {}).get('property'))
                if prop in options and (len(options) == 1) == exact:
                    return variant
    return None


def serialize(variant):
    concept = variant.target_concept
    return {
        'id': variant.pk,
        'axis_key': variant.axis_key,
        'axis_values': variant.axis_values,
        'units': variant.units,
        'occurrence_count': variant.occurrence_count,
        'status': variant.status,
        'target_concept': None if concept is None else {
            'concept_id': concept.concept_id,
            'concept_name': concept.concept_name,
            'concept_code': concept.concept_code,
            'vocabulary_id': concept.vocabulary_id,
        },
        'reviewed_at': variant.reviewed_at.isoformat() if variant.reviewed_at else None,
    }


def set_destination(variant, concept, *, approve, user, reopen=False):
    """Point a variant at ``concept``; approving re-points its stored rows.

    Only rows in the variant's units move, and only off the concept they would
    otherwise carry (the mapping's own destination): records in other units
    belong to other variants.

    Reopening an approved variant (``approve=False, reopen=True``) moves its
    rows back to the mapping's destination (concept 0 when it has none),
    because ingest stops routing to it: leaving them would put two concepts on
    one code and one unit. Without ``reopen`` an approved variant refuses
    ``approve=False``, so picking a new destination cannot reopen it silently.
    """
    from omop_core.mapping.code_resolution import repoint_clinical_rows

    previous = variant.target_concept_id if variant.status == 'approved' else None
    mapping = variant.mapping
    if previous is not None and not approve and not reopen:
        raise ValueError(
            'This variant is approved. Approve its new destination, or reopen it '
            'first -- reopening moves its records back to the code\'s destination.')
    # A code with no destination of its own stored these records at concept
    # 0, OMOP's "No matching concept"; that is where they move off and back to.
    fallback = mapping.target_concept_id if mapping.target_concept_id is not None else 0
    variant.target_concept = concept
    variant.status = 'approved' if approve else 'proposed'
    if approve:
        variant.reviewer = user if getattr(user, 'is_authenticated', False) else None
        variant.reviewed_at = timezone.now()
    variant.save()
    result = None
    if approve and concept is not None:
        old = previous if previous is not None else fallback
        result = repoint_clinical_rows(
            mapping=mapping, old_concept_id=old, new_concept_id=concept.concept_id,
            units=variant.units,
        )
    elif not approve and previous is not None:
        result = repoint_clinical_rows(
            mapping=mapping, old_concept_id=previous, new_concept_id=fallback,
            units=variant.units,
        )
    if approve and not mapping.axis_variant_count:
        # Routing is gated on the count; an approval must switch it on.
        mapping.axis_variant_count = max(FLAG_THRESHOLD, mapping.axis_variants.count())
        mapping.save(update_fields=['axis_variant_count'])
    return result


#: Variants one Suggest click fills in; a code with more has a curator's work.
SUGGEST_MAX = 5


def _locked(mapping):
    """The mapping's row, locked for the rest of the transaction."""
    from omop_core.models import SourceCodeConceptMapping
    return SourceCodeConceptMapping.objects.select_for_update(of=('self',)).get(pk=mapping.pk)


def suggest_destinations(mapping, *, ranking_model, user, strategies=None, overrides=None,
                         limit=SUGGEST_MAX):
    """Suggest a standard LOINC for each variant without one; store it as proposed (#2012).

    Each variant is suggested as the code would be with that variant's axis
    values added to the curator's entries -- the mmol/L variant of a glucose
    code asks for a substance concentration -- so the winner is the LOINC the
    axis filter and the ranker agree on. Only a usable standard LOINC is
    stored; anything else leaves the variant as it was, with the reason in
    ``note``. A variant that already has a destination, proposed by a curator
    or approved, is never touched: variants carry no provenance, so a
    re-suggestion could not tell a curator's pick from an earlier ranker's.

    Variants are stored under the mapping's row lock, like the variant
    endpoint does, so a Suggest racing an approval cannot insert one twice;
    the lock is not held across the ranker calls.

    Returns ``[{axis_key, suggested, confidence, note}]`` for every variant
    considered, ``suggested`` being the stored concept or ``None``.
    """
    from omop_core.mapping.suggestions import suggest_one_mapping
    from omop_core.models import Concept

    counts = unit_counts(mapping)
    with transaction.atomic():
        _locked(mapping)
        stored = refresh(mapping, counts)
    todo = [v for v in stored if v.status != 'approved' and v.target_concept_id is None]
    if len(stored) < FLAG_THRESHOLD or not todo:
        return []
    base = dict(mapping.loinc_axes or {}) if overrides is None else dict(overrides)
    units = observed_units(mapping, counts)
    results = []
    for variant in todo[:max(0, limit)]:
        entry = {'axis_key': variant.axis_key, 'suggested': None, 'confidence': None, 'note': ''}
        results.append(entry)
        try:
            result = suggest_one_mapping(
                mapping.source_code, mapping.source_vocabulary_id, mapping.omop_table,
                source_description=mapping.source_code_description or '',
                row_metadata=mapping.source_metadata, strategies=strategies,
                ranking_model=ranking_model, axis_overrides={**base, **(variant.axis_values or {})},
                observed_units=units,
            )
        except ValueError as exc:  # a code with no OMOP table has no pipeline
            entry['note'] = str(exc)
            continue
        chosen = result.get('suggested')
        concept = Concept.objects.filter(pk=chosen['concept_id']).first() if chosen else None
        if concept is None:
            entry['note'] = result.get('note') or ''
            continue
        if concept.vocabulary_id != 'LOINC' or not usable_destination(concept, mapping):
            entry['note'] = 'The suggested concept is not a standard LOINC, so the variant was left for a curator.'
            continue
        with transaction.atomic():
            _locked(mapping)
            variant.refresh_from_db()
            if variant.status == 'approved' or variant.target_concept_id is not None:
                entry['note'] = 'A destination was set while suggesting; it was kept.'
                continue
            set_destination(variant, concept, approve=False, user=user)
        entry['suggested'] = {
            'concept_id': concept.concept_id, 'concept_name': concept.concept_name,
            'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
        }
        entry['confidence'] = chosen.get('confidence')
        entry['note'] = result.get('note') or ''
    return results


def serialize_all(mapping):
    """Every stored variant of ``mapping``, largest first."""
    return [serialize(v) for v in mapping.axis_variants.select_related('target_concept')
            .order_by('-occurrence_count', 'axis_key')]


def refresh_multi_unit_codes(keys):
    """Refresh only the codes among ``keys`` that can have variants.

    The ingest hook: one grouped query finds the ``(vocabulary, code)`` pairs
    with two or more distinct units across patients, or that already carry a
    count, and only those pay for :func:`refresh`. A 500-code upload of
    single-unit codes costs two queries, not 2,000.
    """
    from django.db.models import Count, Q

    from omop_core.models import PatientSourceCode, SourceCodeConceptMapping

    keys = {(vocab or '', code) for vocab, code in keys if code}
    if not keys:
        return 0
    match = Q()
    for vocab, code in keys:
        match |= Q(source_vocabulary_id=vocab, source_value=code)
    multi = set(
        PatientSourceCode.objects.filter(match).exclude(source_unit='')
        .values('source_vocabulary_id', 'source_value')
        .annotate(units=Count('source_unit', distinct=True))
        .filter(units__gte=FLAG_THRESHOLD)
        .values_list('source_vocabulary_id', 'source_value')
    )
    mapping_match = Q()
    for vocab, code in keys:
        mapping_match |= Q(source_vocabulary_id=vocab, source_code=code)
    flagged = set(
        SourceCodeConceptMapping.objects.filter(mapping_match, axis_variant_count__gt=0)
        .values_list('source_vocabulary_id', 'source_code')
    )
    return refresh_codes(multi | flagged)
