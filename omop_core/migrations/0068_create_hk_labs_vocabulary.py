"""Data migration: create the HK-Labs custom vocabulary for LOINC-unmatched tests."""
from django.db import migrations


def create_hk_labs_vocabulary(apps, schema_editor):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Domain = apps.get_model('omop_core', 'Domain')
    ConceptClass = apps.get_model('omop_core', 'ConceptClass')
    Concept = apps.get_model('omop_core', 'Concept')

    Domain.objects.get_or_create(
        domain_id='Measurement',
        defaults={'domain_name': 'Measurement', 'domain_concept_id': 0},
    )

    ConceptClass.objects.get_or_create(
        concept_class_id='Lab Test',
        defaults={'concept_class_name': 'Lab Test', 'concept_class_concept_id': 0},
    )

    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='HK-Labs',
        defaults={
            'vocabulary_name': 'HealthKey Labs extracted test names',
            'vocabulary_reference': 'https://healthkey.ai',
            'vocabulary_version': '1.0',
            'vocabulary_concept_id': 0,
        },
    )

    # Concept 0 is the OMOP sentinel for "no matching concept".
    # Required by FK constraints when measurement_concept_id=0 (LOINC-unmatched).
    Concept.objects.get_or_create(
        concept_id=0,
        defaults={
            'concept_name': 'No matching concept',
            'domain_id': 'Measurement',
            'vocabulary_id': 'HK-Labs',
            'concept_class_id': 'Lab Test',
            'concept_code': '0',
            'valid_start_date': '1970-01-01',
            'valid_end_date': '2099-12-31',
        },
    )


def remove_hk_labs_vocabulary(apps, schema_editor):
    Vocabulary = apps.get_model('omop_core', 'Vocabulary')
    Vocabulary.objects.filter(vocabulary_id='HK-Labs').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0067_add_care_site_and_loinc_class'),
    ]

    operations = [
        migrations.RunPython(create_hk_labs_vocabulary, remove_hk_labs_vocabulary),
    ]
