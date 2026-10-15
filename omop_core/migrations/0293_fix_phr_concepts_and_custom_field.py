"""Fix PHR field concept mappings and register hematologic_spread as a custom field.

Two corrections to the mappings seeded by 0291:

1. **Blank four wrong concepts.** The SNOMED/LOINC codes originally picked for
   condition.note, condition.care_team, hematologic_spread and
   genetic_test.pathologist_notes are in the wrong OMOP domain or don't match
   the clinical intent. Clear them so the mappings use only source_value for
   identity (concept_id=0 observations, standard OMOP pattern for unmapped
   source text). The three drug_exposure/procedure mappings keep their concepts.

2. **Create a CustomPatientField for hematologic_spread.** An approved
   FieldConceptMapping with a flat (non-dotted) field_name needs a
   CustomPatientField row to become operational — otherwise the write descriptor
   silently discards it. The dotted names (condition.note, drug_exposure.role,
   etc.) are per-event attribute mappings like genetic_mutations.*, so they don't
   need CustomPatientField rows.
"""
import logging

from django.db import migrations

logger = logging.getLogger(__name__)

# The four field_names whose concepts were clinically wrong:
#   condition.note:    LOINC 34109-9 is Document Ontology, not Observation domain
#   condition.care_team: SNOMED 133932002 is "Caregiver (person)", not a care team
#   hematologic_spread:  SNOMED 128462008 is Condition domain, not Observation
#   genetic_test.pathologist_notes: LOINC 22637-3 is AP tumor registry, not genetic test
_FIELDS_TO_BLANK = [
    'condition.note',
    'condition.care_team',
    'hematologic_spread',
    'genetic_test.pathologist_notes',
]


def fix_concepts_and_register_field(apps, schema_editor):
    alias = schema_editor.connection.alias
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    CustomPatientField = apps.get_model('omop_core', 'CustomPatientField')

    # 1. Blank the four wrong concepts.
    blanked = FieldConceptMapping.objects.using(alias).filter(
        field_name__in=_FIELDS_TO_BLANK,
        provenance='system_generated',
    ).update(
        concept=None,
        vocabulary_id='',
        concept_code='',
    )
    if blanked:
        logger.info('Blanked concept on %d PHR field mapping(s).', blanked)

    # 2. Register hematologic_spread as a CustomPatientField.
    mapping = FieldConceptMapping.objects.using(alias).filter(
        field_name='hematologic_spread',
        status='approved',
    ).first()
    if mapping is None:
        logger.warning(
            'No approved mapping for hematologic_spread; '
            'skipping CustomPatientField creation.',
        )
        return

    CustomPatientField.objects.using(alias).update_or_create(
        field_name='hematologic_spread',
        defaults=dict(
            display_name='Hematologic Spread',
            tab='disease',
            field_type='text',
            mode='editable',
            mapping=mapping,
        ),
    )


def reverse(apps, schema_editor):
    alias = schema_editor.connection.alias
    CustomPatientField = apps.get_model('omop_core', 'CustomPatientField')

    # Remove the CustomPatientField; leave the mapping (0291's reverse handles it).
    CustomPatientField.objects.using(alias).filter(
        field_name='hematologic_spread',
    ).delete()

    # Concept restoration is not attempted: the original codes were wrong.
    # Rolling back leaves the mappings with blank concepts, which is safe.


class Migration(migrations.Migration):

    dependencies = [('omop_core', '0292_merge_20261014_1611')]

    operations = [
        migrations.RunPython(fix_concepts_and_register_field, reverse),
    ]
