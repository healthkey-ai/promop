"""Relationship-backed LOINC metadata attached to OMOP concepts."""

from omop_core.services.canonical_units import loinc_axes_for_concepts


def _example_unit_list(value):
    return list(dict.fromkeys(unit.strip() for unit in (value or '').split(';') if unit.strip()))


def loinc_metadata_for_concepts(concepts):
    """Return ``{concept_id: metadata}`` without per-concept queries.

    Athena relationships own class, property and scale. ``LoincCodeClass`` is
    consulted only for ``EXAMPLE_UNITS``, the one field here that Loinc.csv
    supplies and the concept graph does not.
    """
    from omop_core.models import LoincCodeClass

    concepts = [
        concept for concept in concepts
        if concept.vocabulary_id == 'LOINC' and concept.domain_id == 'Measurement'
    ]
    if not concepts:
        return {}

    axes = loinc_axes_for_concepts(concepts)
    codes = {concept.concept_code for concept in concepts}
    units = dict(
        LoincCodeClass.objects
        .filter(loinc_num__in=codes)
        .values_list('loinc_num', 'example_units')
    )
    result = {}
    for concept in concepts:
        concept_axes = axes.get(concept.pk)
        result[concept.pk] = {
            'class_code': concept_axes.class_code if concept_axes else None,
            'class_display': concept_axes.class_name if concept_axes else None,
            'property': (concept_axes.property_code or None) if concept_axes else None,
            'property_display': concept_axes.property_name if concept_axes else None,
            'scale_type': (concept_axes.scale_type_code or None) if concept_axes else None,
            'scale_type_display': concept_axes.scale_type_name if concept_axes else None,
            'example_units': _example_unit_list(units.get(concept.concept_code)),
        }
    return result
