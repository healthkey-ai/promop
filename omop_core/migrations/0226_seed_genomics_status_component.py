"""Seed the genomic finding status component recipe.

LOINC 69548-6 (genetic variant assessment) resolves in the Measurement domain.
Falls back to source key genomics:status with concept 0 when LOINC is not loaded.
"""
from django.db import migrations
from django.utils import timezone


def seed_status(apps, schema_editor):
    using = schema_editor.connection.alias
    Concept = apps.get_model('omop_core', 'Concept')
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    zero = Concept.objects.using(using).filter(pk=0).first()
    standard = Concept.objects.using(using).filter(
        vocabulary_id='LOINC', concept_code='69548-6',
        standard_concept='S', invalid_reason__isnull=True,
    ).first()
    Mapping.objects.using(using).get_or_create(
        field_name='genetic_mutations.status',
        defaults={
            'concept': standard or zero,
            'vocabulary_id': 'LOINC',
            'concept_code': '69548-6',
            'source_value': 'genomics:status',
            'omop_table': standard.domain_id.lower() if standard else 'measurement',
            'value_kind': 'string',
            'unit': '',
            'type_concept_id': 32817,
            'multiple': False,
            'status': 'approved',
            'reviewed_at': timezone.now(),
            'notes': 'Finding status (present/absent/indeterminate). LOINC 69548-6.',
        },
    )


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0225_merge_genomics_and_suggestion_backfill')]
    operations = [migrations.RunPython(seed_status, migrations.RunPython.noop)]
