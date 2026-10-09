"""Version 3 portable variant-name question; preserve curator-owned recipes.

Keep the historical source key and existing facts. Only the exact original
unreviewed seed is eligible. Blank provenance alone is never seed evidence.
No rollback erases a subsequent curator decision.
"""
from django.db import migrations
from django.db.models import Q

_ORIGINAL_NOTES = (
    'Genomics catalog v1: approved storage mapping per implementation request. '
    'Source: CancerBot a840f8d9af2c35477e2b6b76f981b29f02c21eec. '
    'Unmapped source text uses concept 0; clinical nomenclature requires expert review.'
)


def promote_variant_name(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    Mapping.objects.using(schema_editor.connection.alias).filter(
        field_name='genetic_mutations.variant_name',
        vocabulary_id='', concept_code='', source_value='genomics:variant_name',
        omop_table='observation', value_kind='string', unit='', value_vocabulary='',
        type_concept_id=32817, multiple=False, status='approved', reviewer__isnull=True,
        provenance__in=['', 'system_generated'], notes=_ORIGINAL_NOTES,
    ).filter(Q(concept_id=0) | Q(concept_id__isnull=True)).update(
        vocabulary_id='LOINC', concept_code='81253-7', provenance='system_generated',
        notes='Genomics recipe v3: portable variant name (LOINC 81253-7); '
              'historical source key retained. Concept 0 until vocabulary resolution. '
              'Storage recipe only; clinical nomenclature requires expert review.',
    )


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0230_cytogenetic_marker_choices')]
    operations = [migrations.RunPython(promote_variant_name, migrations.RunPython.noop)]
