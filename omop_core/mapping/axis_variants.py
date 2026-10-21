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

from omop_core.mapping import loinc_axes
from omop_core.services.canonical_units import unit_code

#: Codes with this many variants or more are flagged in the queue.
FLAG_THRESHOLD = 2

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
    return '|'.join(f'{axis}={values[axis]}' for axis in loinc_axes.AXES if axis in values)


def _unit_counts(mapping):
    """``Counter`` of normalized units for one mapping, from every evidence source."""
    from omop_core.models import PatientSourceCode
    from omop_core.services.mapping_unit_consistency import (
        _imported_unit_counts, _unit_counts as live_unit_counts,
    )

    counts = Counter()
    if mapping.omop_table == 'measurement':
        # Union, not sum: the inventory and the stored rows can describe the
        # same records, as the approval unit check documents.
        counts = (live_unit_counts([mapping]).get(mapping.pk, Counter())
                  | _imported_unit_counts([mapping]).get(mapping.pk, Counter()))
    patient_units = Counter()
    rows = PatientSourceCode.objects.filter(
        source_value=mapping.source_code,
        source_vocabulary_id=mapping.source_vocabulary_id,
    )
    if mapping.omop_table:
        rows = rows.filter(omop_table=mapping.omop_table)
    for unit, override, count in rows.values_list('source_unit', 'source_unit_override', 'occurrence_count'):
        normalized = unit_code(override if override is not None else unit)
        if normalized:
            patient_units[normalized] += max(1, count or 0)
    return counts | patient_units


def _specimen_systems(mapping):
    """``Counter`` of LOINC systems the per-patient metadata names."""
    from omop_core.models import PatientSourceCode

    systems = Counter()
    rows = PatientSourceCode.objects.filter(
        source_value=mapping.source_code,
        source_vocabulary_id=mapping.source_vocabulary_id,
    ).exclude(source_metadata={})
    for metadata, count in rows.values_list('source_metadata', 'occurrence_count')[:2000]:
        resource = loinc_axes._resource(metadata)
        specimen = loinc_axes._text_of(resource.get('specimen'))
        system = loinc_axes.SPECIMEN_SYSTEMS.get(loinc_axes._norm(specimen)) if specimen else None
        if system:
            systems[system] += max(1, count or 0)
    return systems


def detect(mapping, unit_counts=None):
    """``(variants, notes)`` for one mapping.

    ``variants`` is a list of ``{axis_key, axis_values, units, occurrence_count}``
    ordered by occurrence, largest first. A code whose units all imply the same
    axes has one variant -- which is not a variant at all, and is what most
    codes look like. ``notes`` describes variation that cannot be routed.
    """
    groups = defaultdict(lambda: {'units': Counter(), 'axis_values': {}})
    counts = unit_counts if unit_counts is not None else _unit_counts(mapping)
    for unit, count in counts.items():
        prop = unit_property(unit)
        # A unit with no single property is still a quantity, but nothing says
        # which group it joins, so it is a variant of its own, keyed by unit.
        values = {'property': prop, 'scale_type': 'Qn'} if prop else {'scale_type': 'Qn'}
        key = axis_key(values) if prop else f'unit={unit}'
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

    notes = []
    systems = _specimen_systems(mapping)
    if len(systems) >= 2:
        notes.append(
            'Specimens differ across patients ('
            + ', '.join(f'{system} ×{count}' for system, count in systems.most_common())
            + '). Ingest cannot route on specimen, so these share a destination.'
        )
    return variants, notes


@transaction.atomic
def refresh(mapping):
    """Store the detected variants and the count the queue flags.

    Existing variants keep their destination and status: a re-detection must
    not undo a curator's choice. Variants no longer seen are kept too, because
    their units may simply not have arrived in the latest load. Returns the
    variants as stored.
    """
    from omop_core.models import SourceCodeAxisVariant

    variants, _notes = detect(mapping)
    existing = {v.axis_key: v for v in mapping.axis_variants.all()}
    if len(variants) >= FLAG_THRESHOLD:
        for variant in variants:
            row = existing.get(variant['axis_key'])
            if row is None:
                row = SourceCodeAxisVariant(mapping=mapping, axis_key=variant['axis_key'])
            row.axis_values = variant['axis_values']
            row.units = variant['units']
            row.occurrence_count = variant['occurrence_count']
            row.save()
            existing[variant['axis_key']] = row
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


def refresh_codes(keys):
    """Refresh every mapping for ``(source_vocabulary_id, source_code)`` keys.

    Failures are logged, not raised: this runs after ingest writes, and a
    variant count is curation evidence, not a reason to fail an upload.
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
    for mapping in SourceCodeConceptMapping.objects.filter(match):
        try:
            refresh(mapping)
            refreshed += 1
        except Exception:  # noqa: BLE001 - evidence must not fail ingest
            logging.getLogger(__name__).warning(
                'Axis variant refresh failed for mapping %s.', mapping.pk, exc_info=True)
    return refreshed


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
                if usable_destination(v.target_concept, mapping)]
    for variant in approved:
        if unit in (variant.units or []):
            return variant
    if prop:
        for variant in approved:
            if (variant.axis_values or {}).get('property') == prop:
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


def set_destination(variant, concept, *, approve, user):
    """Point a variant at ``concept``; approving re-points its stored rows.

    Only rows in the variant's units move, and only off the concept they would
    otherwise carry (the mapping's own destination): records in other units
    belong to other variants.

    Reopening an approved variant (``approve=False``) moves its rows back to
    the mapping's destination, because ingest stops routing to it: leaving
    them would put two concepts on one code and one unit.
    """
    from omop_core.mapping.code_resolution import repoint_clinical_rows

    previous = variant.target_concept_id if variant.status == 'approved' else None
    mapping = variant.mapping
    if previous is not None and not approve and mapping.target_concept_id is None:
        raise ValueError(
            "This variant is approved and the code has no destination of its own "
            "to move its records back to. Approve a new destination instead.")
    variant.target_concept = concept
    variant.status = 'approved' if approve else 'proposed'
    if approve:
        variant.reviewer = user if getattr(user, 'is_authenticated', False) else None
        variant.reviewed_at = timezone.now()
    variant.save()
    result = None
    if approve and concept is not None:
        old = previous if previous is not None else mapping.target_concept_id
        result = repoint_clinical_rows(
            mapping=mapping, old_concept_id=old, new_concept_id=concept.concept_id,
            units=variant.units,
        )
    elif not approve and previous is not None:
        result = repoint_clinical_rows(
            mapping=mapping, old_concept_id=previous, new_concept_id=mapping.target_concept_id,
            units=variant.units,
        )
    if approve and not mapping.axis_variant_count:
        # Routing is gated on the count; an approval must switch it on.
        mapping.axis_variant_count = max(FLAG_THRESHOLD, mapping.axis_variants.count())
        mapping.save(update_fields=['axis_variant_count'])
    return result


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
