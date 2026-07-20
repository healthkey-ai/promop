"""Seed the single cdm_source row so the instance self-describes as CDM 5.4.

The cdm_source table lets OHDSI tooling read the CDM version and holder. This
seeds one describing row for the PRomop instance. Idempotent on cdm_source_abbreviation.
"""
from datetime import date

from django.db import migrations

# Standard vocabulary concept for "OMOP CDM Version 5.4.0". Only linked if the
# concept is present (the FK is nullable), so this runs on instances whose
# vocabulary has not been loaded.
_CDM_54_VERSION_CONCEPT_ID = 756265


def seed_cdm_source(apps, schema_editor):
    CdmSource = apps.get_model('omop_core', 'CdmSource')
    Concept = apps.get_model('omop_core', 'Concept')

    version_concept_id = None
    if Concept.objects.filter(concept_id=_CDM_54_VERSION_CONCEPT_ID).exists():
        version_concept_id = _CDM_54_VERSION_CONCEPT_ID

    CdmSource.objects.get_or_create(
        cdm_source_abbreviation='PRomop',
        defaults={
            'cdm_source_name': 'PRomop — Decision-Ready Longitudinal Patient Record',
            'cdm_holder': 'HealthKey, Inc.',
            'source_description': (
                'PRomop longitudinal patient record on the OMOP CDM 5.4 clinical '
                'tables with OHDSI oncology extensions and a derived PatientRecord projection.'
            ),
            'source_documentation_reference': 'https://github.com/healthkey-ai',
            'cdm_etl_reference': 'https://github.com/healthkey-ai',
            'cdm_release_date': date(2026, 1, 1),
            'cdm_version': '5.4',
            'cdm_version_concept_id': version_concept_id,
        },
    )


def unseed_cdm_source(apps, schema_editor):
    CdmSource = apps.get_model('omop_core', 'CdmSource')
    CdmSource.objects.filter(cdm_source_abbreviation='PRomop').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0111_cdmsource_sourcetoconceptmap_observationperiod_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_cdm_source, unseed_cdm_source),
    ]
