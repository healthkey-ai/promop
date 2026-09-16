from django.db import migrations
from django.db.models import F


def backfill_approved_suggestion_outcomes(apps, schema_editor):
    """Count verified legacy Suggest approvals as model acceptances."""
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    Mapping.objects.filter(
        origin_system__istartswith='suggest',
        suggestion_model_version__gt='',
        suggestion_outcome='',
        status='approved',
        target_concept__isnull=False,
        suggested_target_concept_id=F('target_concept_id'),
    ).update(suggestion_outcome='accepted')


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0222_trial_favorites_and_search_preferences')]
    operations = [
        migrations.RunPython(
            backfill_approved_suggestion_outcomes,
            migrations.RunPython.noop,
        ),
    ]
