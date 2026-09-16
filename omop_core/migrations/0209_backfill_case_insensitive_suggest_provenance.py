from django.db import migrations
from django.db.models import F


def backfill_suggest_provenance(apps, schema_editor):
    """Correct pre-v0.1 rows whose UI provenance was capitalised as Suggest."""
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    legacy = Mapping.objects.filter(origin_system__iexact='suggest')
    legacy.update(origin_system='suggest v0.1', suggestion_model_version='v0.1')
    legacy.filter(suggested_target_concept__isnull=True, target_concept__isnull=False).update(
        suggested_target_concept_id=F('target_concept_id')
    )


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0208_suggestion_model_version')]
    operations = [migrations.RunPython(backfill_suggest_provenance, migrations.RunPython.noop)]
