"""Index observed hospital names without treating source-system URIs as owners."""

from django.db import migrations, models


def queue_registry_backfill(apps, schema_editor):
    """Replay the pinned seed asynchronously if it was already applied.

    Sites with the v2 seed still queued need no second job: the updated importer
    writes the registry as part of that original import.
    """
    HospitalCodeImport = apps.get_model('omop_core', 'HospitalCodeImport')
    HospitalCodeImport.objects.filter(
        artifact_identity='healthtree-hospital-seed-20261005-v2',
        outcome='applied',
    ).update(
        outcome='queued', task_id='', stats={}, failure_reason='',
        started_at=None, completed_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0290_personmergecandidate'),
        ('omop_core', '0289_merge_patient_source_overrides_and_resolve_created'),
    ]

    operations = [
        migrations.CreateModel(
            name='SourceSystemFacility',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('source_vocabulary_id', models.CharField(db_index=True, max_length=255)),
                ('facility_name', models.TextField()),
                ('facility_id', models.TextField(blank=True, default='')),
                ('level', models.CharField(blank=True, default='', max_length=32)),
                ('confidence', models.CharField(blank=True, default='', max_length=32)),
                ('attribution_method', models.CharField(blank=True, default='', max_length=100)),
                ('provenance', models.CharField(max_length=255)),
                ('fingerprint', models.CharField(max_length=64, unique=True)),
            ],
            options={'db_table': 'source_system_facility'},
        ),
        migrations.RunPython(queue_registry_backfill, migrations.RunPython.noop),
    ]
