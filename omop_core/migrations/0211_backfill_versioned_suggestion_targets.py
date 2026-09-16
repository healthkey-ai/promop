from django.db import migrations
from django.db.models import F


def backfill_versioned_suggestion_targets(apps, schema_editor):
    """Keep legacy versioned proposals reviewable even after provenance changed."""
    Mapping = apps.get_model('omop_core', 'SourceCodeConceptMapping')
    versioned = Mapping.objects.filter(
        origin_system__istartswith='suggest v',
        suggested_target_concept__isnull=True,
        target_concept__isnull=False,
    )
    # Populate the immutable evidence before any provenance changes.  The
    # original 0.1 migration did this in the other order, so its filtered
    # queryset no longer matched after update().
    versioned.update(suggested_target_concept_id=F('target_concept_id'))


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0210_merge_20260905_1541')]
    operations = [migrations.RunPython(backfill_versioned_suggestion_targets, migrations.RunPython.noop)]
