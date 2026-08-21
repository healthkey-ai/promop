"""
field_descriptor.py — Classify every PatientRecord field by its mapping status.

Used by the concept-mapping admin interface to show which fields have OMOP
concept assignments and which still need one.
"""
from __future__ import annotations

from omop_core.models import PatientRecord, FieldConceptMapping
from omop_core.services.mappings import (
    LAB_FIELD_TO_LOINC,
    LAB_FIELD_ALIAS_TO_CANONICAL,
    DEMOGRAPHIC_FIELDS,
    THERAPY_LINE_FIELDS,
)
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _LAB_FIELD_ALIASES,
    _LOINC_LAB_FIELDS,
)
from omop_core.services.provenance_registry import get_registry


# Fields that are purely internal / structural and not clinical.
_INTERNAL_FIELDS = frozenset({
    'id', 'person', 'organization', 'created_at', 'updated_at',
    'derivation_version', 'derived_at', 'user_edited_fields',
})

# Person/profile fields projected from Person model.
_PERSON_FIELDS = frozenset({
    'date_of_birth', 'gender', 'race', 'ethnicity', 'languages_skills',
    'email', 'phone_number', 'facility_name', 'validated', 'validated_by',
    'validation_date', 'suppress_demographics_for_others', 'patient_age',
})

# Location fields.
_LOCATION_FIELDS = frozenset({
    'country', 'region', 'city', 'postal_code', 'latitude', 'longitude',
})

# Wearable 30-day summary fields.
_WEARABLE_30D_SUFFIX = '_30d'
_WEARABLE_METADATA_FIELDS = frozenset({
    'wearable_last_sync_at', 'wearable_coverage_ratio_30d',
})

# Computed fields (derived from other fields, not directly from OMOP).
_COMPUTED_FIELDS = frozenset({
    'bmi', 'disease_slug', 'therapy_lines_count',
    'meets_crab', 'meets_slim',
})

# Unit-companion fields (always paired with a measurement field).
_UNIT_SUFFIX = '_units'

# Build the set of all alias field names.
_ALL_ALIASES = set(LAB_FIELD_ALIAS_TO_CANONICAL.keys())
for aliases in _LAB_FIELD_ALIASES.values():
    _ALL_ALIASES.update(aliases)

# Fields that are editable via the LOINC lab write-through: either in
# LAB_FIELD_TO_LOINC (the write map) or as a target in _LOINC_LAB_FIELDS
# (the derivation map, which names fields the extractor can populate via a
# known LOINC code — those 18 recovered attributions from #596/#607).
_LOINC_EDITABLE_FIELDS = frozenset(LAB_FIELD_TO_LOINC.keys()) | frozenset(
    field_name for field_name, _cast in _LOINC_LAB_FIELDS.values()
)


def _get_field_type_label(field) -> str:
    """Return a human-readable type label for a Django model field."""
    type_map = {
        'CharField': 'text',
        'TextField': 'text',
        'IntegerField': 'integer',
        'FloatField': 'float',
        'DecimalField': 'decimal',
        'BooleanField': 'boolean',
        'NullBooleanField': 'boolean',
        'DateField': 'date',
        'DateTimeField': 'datetime',
        'JSONField': 'json',
        'ForeignKey': 'fk',
        'BigIntegerField': 'integer',
        'SmallIntegerField': 'integer',
        'PositiveIntegerField': 'integer',
    }
    class_name = field.__class__.__name__
    return type_map.get(class_name, class_name.lower())


def _classify_field(field_name: str) -> str:
    """Assign a category to a PatientRecord field."""
    if field_name in _INTERNAL_FIELDS:
        return 'internal'
    if field_name in _PERSON_FIELDS:
        return 'profile'
    if field_name in _LOCATION_FIELDS:
        return 'location'
    if field_name in _LOINC_EDITABLE_FIELDS:
        return 'editable'
    if field_name in _ALL_ALIASES:
        return 'alias'
    if field_name.endswith(_UNIT_SUFFIX):
        return 'unit'
    if field_name in DEMOGRAPHIC_FIELDS:
        return 'profile'
    if field_name in THERAPY_LINE_FIELDS:
        return 'therapy-inference'
    if field_name in _COMPUTED_FIELDS:
        return 'computed'
    if field_name in _WEARABLE_METADATA_FIELDS:
        return 'wearable-metadata'
    if field_name.endswith(_WEARABLE_30D_SUFFIX):
        return 'computed'
    # Therapy-related fields not in THERAPY_LINE_FIELDS but containing therapy keywords.
    therapy_keywords = (
        'therapy', 'treatment', 'line_', 'component_ids', 'therapy_type_ids',
        'therapy_ids', 'concomitant_medication',
    )
    if any(kw in field_name for kw in therapy_keywords):
        return 'therapy-inference'
    if field_name in PATIENT_RECORD_OMOP_MAPPED_FIELDS:
        return 'needs-concept-set'
    return 'other'


def get_all_field_descriptors() -> list[dict]:
    """Return a descriptor dict for every concrete PatientRecord field.

    Each dict contains:
      - field_name: str
      - field_type: str (human-readable type label)
      - category: str (editable|alias|unit|profile|location|therapy-inference|
                       computed|wearable-metadata|needs-concept-set|
                       internal|other)
      - provenance: dict|None (from provenance registry)
      - mapping: dict|None (from FieldConceptMapping table)
    """
    # 1. Get all concrete fields from the model.
    concrete_fields = [
        f for f in PatientRecord._meta.get_fields()
        if getattr(f, 'concrete', False)
    ]

    # 2. Load provenance registry.
    registry = get_registry()

    # 3. Load all existing FieldConceptMapping rows.
    mappings_by_field = {
        m.field_name: m
        for m in FieldConceptMapping.objects.select_related('concept', 'reviewer').all()
    }

    # 4. Build descriptors.
    result = []
    for f in concrete_fields:
        name = f.name
        category = _classify_field(name)

        # Provenance from registry.
        prov = registry.get(name)
        prov_dict = None
        if prov:
            prov_dict = {
                'omop_table': prov.omop_table,
                'lookup_strategy': prov.lookup_strategy,
                'concept_codes': prov.concept_codes,
                'source_values': prov.source_values,
                'extractor': prov.extractor,
                'selection_rule': prov.selection_rule,
                'description': prov.description,
            }

        # FieldConceptMapping from DB.
        mapping = mappings_by_field.get(name)
        mapping_dict = None
        if mapping:
            mapping_dict = {
                'id': mapping.id,
                'concept_id': mapping.concept_id,
                'vocabulary_id': mapping.vocabulary_id,
                'concept_code': mapping.concept_code,
                'unit': mapping.unit,
                'omop_table': mapping.omop_table,
                'status': mapping.status,
                'reviewer': mapping.reviewer.username if mapping.reviewer else None,
                'reviewed_at': mapping.reviewed_at.isoformat() if mapping.reviewed_at else None,
                'notes': mapping.notes,
            }

        result.append({
            'field_name': name,
            'field_type': _get_field_type_label(f),
            'category': category,
            'provenance': prov_dict,
            'mapping': mapping_dict,
        })

    # Sort: needs-concept-set first, then by field name.
    category_order = {
        'needs-concept-set': 0,
        'editable': 1,
        'therapy-inference': 2,
        'computed': 3,
        'alias': 4,
        'unit': 5,
        'profile': 6,
        'location': 7,
        'wearable-metadata': 8,
        'internal': 9,
        'other': 10,
    }
    result.sort(key=lambda d: (category_order.get(d['category'], 99), d['field_name']))
    return result
