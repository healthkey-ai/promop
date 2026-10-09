"""Make employment_status writable, which it already derives.

`_get_social_data` has always read this field back from an Observation whose
concept is SNOMED 224362002 "Employment status", taking the answer from
value_as_string. What was missing was a mapping row saying so, so the descriptor
reported the field unmapped and the editor fell back to patching the projection,
which refuses it.

This is the shape the mapping table is for: the read path existed, the write path
existed, and nothing connected them.

Deliberately narrow. Twenty-four of the twenty-seven fields on the same tab have
no derivation extractor at all — nothing in patient_record_service ever assigns
them — so a mapping row for those would create a write whose value never comes
back. A mapping is only worth seeding where something reads the result.
"""
from django.db import migrations

_EMPLOYMENT_SNOMED = '224362002'


def seed(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Concept = apps.get_model('omop_core', 'Concept')

    concept = Concept.objects.filter(
        vocabulary_id='SNOMED', concept_code=_EMPLOYMENT_SNOMED,
    ).first()
    if concept is None:
        # Vocabularies load separately; without the concept a mapping would look
        # curated and write nothing. The field stays unmapped until it is there.
        return

    FieldConceptMapping.objects.update_or_create(
        field_name='employment_status',
        defaults={
            'concept': concept,
            'vocabulary_id': 'SNOMED',
            'concept_code': _EMPLOYMENT_SNOMED,
            'omop_table': 'observation',
            # Derivation matches on the concept code; keeping the source value
            # equal to it means a hand edit and an import look the same.
            'source_value': _EMPLOYMENT_SNOMED,
            'value_kind': 'string',
            'status': 'approved',
            'notes': (
                'Seeded by migration 0157. _get_social_data already read this '
                'back from SNOMED 224362002; the mapping is what lets an editor '
                'write it.'
            ),
        },
    )


def unseed(apps, schema_editor):
    apps.get_model('omop_core', 'FieldConceptMapping').objects.filter(
        field_name='employment_status',
    ).delete()


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0156_seed_sct_field_mappings')]
    operations = [migrations.RunPython(seed, unseed)]
