from django.db import migrations, models
from django.db.models import F


def mark_legacy_suggestions(apps, schema_editor):
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    legacy = Mapping.objects.filter(origin_system='suggest')
    legacy.update(origin_system='suggest v0.1', suggestion_model_version='v0.1')
    legacy.filter(suggested_target_concept__isnull=True, target_concept__isnull=False).update(
        suggested_target_concept_id=F('target_concept_id')
    )


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0207_merge_20260905_1053')]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='suggestion_model_version',
            field=models.CharField(blank=True, db_index=True, default='', max_length=20),
        ),
        migrations.RunPython(mark_legacy_suggestions, migrations.RunPython.noop),
    ]
