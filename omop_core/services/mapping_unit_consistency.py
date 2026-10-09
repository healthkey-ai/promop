"""Check whether stored source units fit a mapping's LOINC destination.

This is curation evidence, not an ingest validator.  A warning can be
explicitly accepted because LOINC example units are illustrative and local
feeds legitimately carry units the release does not list.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count
from django.db.models.functions import Lower

from omop_core.models import (
    CanonicalUnitPreference,
    LoincCodeClass,
    Measurement,
    ProvenanceRecord,
)
from omop_core.services.canonical_units import (
    UNIT_GROUPS,
    loinc_axes_for_concepts,
    property_for,
    unit_code,
)


def _source_keys(mapping):
    """Stored Measurement source values that can belong to ``mapping``."""
    width = Measurement._meta.get_field('measurement_source_value').max_length or 50
    keys = {mapping.source_code[:width]} if mapping.source_code else set()
    # Keep this aligned with code_resolution._source_value_match.  Curator prose
    # is not a safe clinical-row key; an import's display text is.
    if mapping.origin == 'import' and mapping.source_code_description:
        keys.add(mapping.source_code_description[:width])
    # Match the SQL Lower() expression below. Python casefolding is stronger
    # (for example, ß -> ss) and would produce keys PostgreSQL LOWER never can.
    return {value.lower() for value in keys if value}


def _split_units(value):
    return [part.strip() for part in (value or '').split(';') if part.strip()]


def _unit_counts(mappings):
    """Return normalized source-unit counts keyed by mapping id.

    The clinical read is one aggregate query globally plus one per hospital
    represented in the batch.  That keeps a 2,557-code group approval from
    becoming 2,557 Measurement queries while preserving organization scope.
    """
    mapping_keys = {mapping.pk: _source_keys(mapping) for mapping in mappings}
    wanted_keys = set().union(*mapping_keys.values()) if mapping_keys else set()
    if not wanted_keys:
        return {mapping.pk: Counter() for mapping in mappings}

    base = (
        Measurement.objects
        .filter(is_erroneous=False)
        .exclude(unit_source_value__isnull=True)
        .exclude(unit_source_value='')
        .annotate(source_key=Lower('measurement_source_value'))
        .filter(source_key__in=wanted_keys)
    )

    def aggregate(queryset):
        by_key = defaultdict(Counter)
        for source_key, raw_unit, count in (
            queryset.values_list('source_key', 'unit_source_value')
            .annotate(count=Count('pk'))
        ):
            normalized = unit_code(raw_unit)
            if normalized:
                by_key[source_key][normalized] += count
        return by_key

    by_scope = {None: aggregate(base)}
    organization_ids = {mapping.organization_id for mapping in mappings if mapping.organization_id}
    if organization_ids:
        content_type = ContentType.objects.get_for_model(Measurement)
        for organization_id in organization_ids:
            attributed_ids = ProvenanceRecord.objects.filter(
                content_type=content_type,
                organization_id=organization_id,
            ).values('object_id')
            by_scope[organization_id] = aggregate(base.filter(pk__in=attributed_ids))

    result = {}
    for mapping in mappings:
        counts = Counter()
        scoped = by_scope.get(mapping.organization_id, {})
        for key in mapping_keys[mapping.pk]:
            counts.update(scoped.get(key, {}))
        result[mapping.pk] = counts
    return result


def _imported_unit_counts(mappings):
    """Normalize unit evidence carried by an imported source-code inventory."""
    result = {}
    for mapping in mappings:
        counts = Counter()
        for evidence in mapping.source_unit_evidence or []:
            if not isinstance(evidence, dict):
                continue
            normalized = unit_code(evidence.get('code') or evidence.get('display'))
            try:
                count = max(0, int(evidence.get('count') or 0))
            except (TypeError, ValueError):
                count = 0
            if normalized and count:
                counts[normalized] += count
        result[mapping.pk] = counts
    return result


def unit_consistency_for_mappings(mappings, *, destination=None):
    """Return unit evidence for mappings, optionally against one destination.

    ``warning`` means a curator must consciously confirm approval.  It never
    means approval is forbidden.
    """
    mappings = list(mappings)
    concepts = {
        (destination or mapping.target_concept).pk: destination or mapping.target_concept
        for mapping in mappings
        if destination is not None or mapping.target_concept is not None
    }
    loinc_concepts = [
        concept for concept in concepts.values()
        if concept.vocabulary_id == 'LOINC' and concept.domain_id == 'Measurement'
    ]
    axes = loinc_axes_for_concepts(loinc_concepts)
    metadata = LoincCodeClass.objects.filter(
        loinc_num__in=[concept.concept_code for concept in loinc_concepts],
    ).in_bulk()
    preferences = CanonicalUnitPreference.objects.filter(
        concept_id__in=concepts,
    ).in_bulk()
    measurement_mappings = [
        mapping for mapping in mappings if mapping.omop_table == 'measurement'
    ]
    live_units = _unit_counts(measurement_mappings)
    imported_units = _imported_unit_counts(measurement_mappings)
    # Independent exports can describe the same clinical records. Counter's
    # union takes the larger count per normalized unit, retaining new live
    # units without inflating overlapping evidence by summing it twice.
    observed_by_mapping = {
        mapping.pk: live_units.get(mapping.pk, Counter())
        | imported_units.get(mapping.pk, Counter())
        for mapping in measurement_mappings
    }

    reports = []
    for mapping in mappings:
        concept = destination or mapping.target_concept
        observed = observed_by_mapping.get(mapping.pk, Counter())
        applicable = bool(
            mapping.omop_table == 'measurement'
            and concept is not None
            and concept.vocabulary_id == 'LOINC'
            and concept.domain_id == 'Measurement'
            and observed
        )
        if not applicable:
            reports.append({
                'mapping_id': mapping.pk,
                'source_vocabulary_id': mapping.source_vocabulary_id,
                'source_code': mapping.source_code,
                'destination_concept_id': concept.pk if concept else None,
                'property': '',
                'expected_units': [],
                'observed_units': [],
                'warning': False,
                'reason': '',
            })
            continue

        concept_metadata = metadata.get(concept.concept_code)
        property_code = property_for(concept, concept_metadata, axes.get(concept.pk))
        expected = _split_units(concept_metadata.example_units if concept_metadata else '')
        preference = preferences.get(concept.pk)
        if (
            preference and preference.unit
            and preference.property == property_code
            and preference.unit in UNIT_GROUPS.get(property_code, {})
        ):
            expected.append(preference.unit)
        expected = list(dict.fromkeys(unit_code(value) for value in expected if unit_code(value)))
        convertible = set(UNIT_GROUPS.get(property_code, {}))

        observed_units = []
        for observed_unit, count in sorted(observed.items()):
            compatible = bool(property_code) and (
                observed_unit in expected or observed_unit in convertible
            )
            observed_units.append({
                'unit': observed_unit,
                'count': count,
                'compatible': compatible,
            })
        warning = any(not row['compatible'] for row in observed_units)
        if warning and not property_code:
            reason = 'destination_not_quantitative'
        elif warning:
            reason = 'incompatible_units'
        else:
            reason = ''
        reports.append({
            'mapping_id': mapping.pk,
            'source_vocabulary_id': mapping.source_vocabulary_id,
            'source_code': mapping.source_code,
            'destination_concept_id': concept.pk,
            'destination_concept_name': concept.concept_name,
            'property': property_code,
            'expected_units': expected,
            'observed_units': observed_units,
            'warning': warning,
            'reason': reason,
        })
    return reports


def unit_consistency_for_mapping(mapping, destination):
    return unit_consistency_for_mappings([mapping], destination=destination)[0]


def summarize_unit_warnings(reports):
    """Compact a batch result for the group-approval confirmation response."""
    warnings = [report for report in reports if report['warning']]
    observed = Counter()
    for report in warnings:
        units_seen_for_mapping = set()
        for row in report['observed_units']:
            if not row['compatible']:
                units_seen_for_mapping.add(row['unit'])
        observed.update(units_seen_for_mapping)
    first = warnings[0] if warnings else {}
    return {
        'warning': bool(warnings),
        'mapping_count': len(reports),
        'warning_count': len(warnings),
        'destination_concept_id': first.get('destination_concept_id'),
        'destination_concept_name': first.get('destination_concept_name', ''),
        'property': first.get('property', ''),
        'expected_units': first.get('expected_units', []),
        'observed_units': [
            {'unit': unit, 'count': count} for unit, count in sorted(observed.items())
        ],
        'observed_unit_count_kind': 'mappings',
        'mapping_ids': [report['mapping_id'] for report in warnings[:50]],
        'mapping_ids_truncated': len(warnings) > 50,
    }
