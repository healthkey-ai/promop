"""Athena takes precedence for each source code on the merged ICD-10 tab."""
from django.db import migrations
from django.db.models.functions import Lower, Trim


def remove_duplicates(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    mappings = Mapping.objects.using(schema_editor.connection.alias).filter(
        source_vocabulary_id__in=['ICD10', 'ICD10CM'],
    ).annotate(normalized_code=Lower(Trim('source_code')))
    athena_codes = list(mappings.filter(
        origin_system='athena', target_concept_id__isnull=False,
    ).exclude(normalized_code='').order_by().values_list('normalized_code', flat=True).distinct())
    # Bound statement size while preserving every Athena row (including 1:N).
    for start in range(0, len(athena_codes), 1000):
        mappings.exclude(origin_system='athena').filter(
            normalized_code__in=athena_codes[start:start + 1000],
        ).delete()


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0215_merge_mapping_destinations')]
    # Deleted redundant rows cannot be reconstructed on rollback.
    operations = [migrations.RunPython(remove_duplicates)]
