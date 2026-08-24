"""Make behavior fields writable where derivation already names the code.

These fields are the same shape as ``employment_status``: the extractor already
reads a dated OMOP row keyed by a reviewed LOINC/SNOMED code and projects the
answer into PatientRecord. The missing piece is the mapping row that lets the
editor write the row derivation reads.

Companion fields are deliberately excluded. ``pregnancy_test_date`` is the event
date of ``pregnancy_test_result_value``; ``substance_use_details`` and
``geographic_exposure_risk_details`` qualify their boolean assertion row. Making
those independent writable fields would create a second same-day row under the
same source value and break the one-field/one-fact contract.
"""
from django.db import migrations


_LAB_TYPE_CONCEPT_ID = 32856
_EHR_TYPE_CONCEPT_ID = 32817

# field_name, concept_code, table, value_kind, type_concept_id
_BEHAVIOR_MAPPINGS = [
    ('smoking_status', '72166-2', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('pack_years', '63640-7', 'measurement', 'number', _LAB_TYPE_CONCEPT_ID),
    ('alcohol_use', '74013-4', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('drinks_per_week', '11286-7', 'measurement', 'number', _LAB_TYPE_CONCEPT_ID),
    ('exercise_frequency', '68516-4', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('exercise_minutes_per_week', '89555-7', 'measurement', 'number', _LAB_TYPE_CONCEPT_ID),
    ('diet_type', '88365-2', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('sleep_hours_per_night', '93832-4', 'measurement', 'number', _LAB_TYPE_CONCEPT_ID),
    ('sleep_quality', '93831-6', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('stress_level', '73985-4', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('social_support', '93033-9', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('education_level', '82589-3', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('marital_status', '45404-1', 'measurement', 'string', _LAB_TYPE_CONCEPT_ID),
    ('number_of_dependents', '63512-8', 'measurement', 'number', _LAB_TYPE_CONCEPT_ID),
    ('annual_household_income', '77243-3', 'measurement', 'number', _LAB_TYPE_CONCEPT_ID),
    ('pregnancy_test_result_value', '2106-3', 'observation', 'string', _EHR_TYPE_CONCEPT_ID),
    ('contraceptive_use', '8659-8', 'observation', 'boolean', _EHR_TYPE_CONCEPT_ID),
    ('consent_capability', '75985-6', 'observation', 'boolean', _EHR_TYPE_CONCEPT_ID),
    ('caregiver_availability_status', '74014-2', 'observation', 'boolean', _EHR_TYPE_CONCEPT_ID),
    ('no_mental_health_disorder_status', '75618-3', 'observation', 'boolean', _EHR_TYPE_CONCEPT_ID),
    ('no_substance_use_status', '74204-0', 'observation', 'boolean', _EHR_TYPE_CONCEPT_ID),
    ('no_geographic_exposure_risk', '82593-5', 'observation', 'boolean', _EHR_TYPE_CONCEPT_ID),
]


def seed(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')

    for field_name, code, table, value_kind, type_concept_id in _BEHAVIOR_MAPPINGS:
        concept = Concept.objects.filter(
            vocabulary_id='LOINC',
            concept_code=code,
        ).first()
        if concept is None:
            # Athena vocabularies load separately. Without the concept, the
            # descriptor must keep the field read-only rather than emitting a
            # write recipe with no target concept.
            continue

        existing = FieldConceptMapping.objects.filter(field_name=field_name).first()
        if existing is not None and existing.status not in ('proposed', ''):
            continue

        FieldConceptMapping.objects.update_or_create(
            field_name=field_name,
            defaults={
                'concept': concept,
                'vocabulary_id': 'LOINC',
                'concept_code': code,
                'omop_table': table,
                'source_value': code,
                'value_kind': value_kind,
                'type_concept_id': type_concept_id,
                'status': 'approved',
                'notes': (
                    'Seeded by migration 0160. patient_record_service already '
                    f'reads this field from LOINC {code}; the mapping lets the '
                    'editor write the same fact.'
                ),
            },
        )


def unseed(apps, schema_editor):
    apps.get_model('omop_core', 'FieldConceptMapping').objects.filter(
        field_name__in=[m[0] for m in _BEHAVIOR_MAPPINGS],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0159_allow_compound_field_mappings')]
    operations = [migrations.RunPython(seed, unseed)]
