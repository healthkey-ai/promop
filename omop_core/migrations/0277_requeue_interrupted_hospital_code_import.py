from django.db import migrations


ARTIFACT_IDENTITY = 'healthtree-hospital-seed-20261002-v1'


def requeue_interrupted_import(apps, schema_editor):
    """Recover the v1 staging receipt stranded by a worker replacement.

    Downstream installations applying 0276 and 0277 together still have a
    queued receipt, so this is a no-op there.  Completed and explicitly failed
    receipts are never changed.
    """
    HospitalCodeImport = apps.get_model('omop_core', 'HospitalCodeImport')
    HospitalCodeImport.objects.filter(
        artifact_identity=ARTIFACT_IDENTITY,
        outcome='running',
        completed_at__isnull=True,
    ).update(
        outcome='queued',
        task_id='',
        stats={},
        failure_reason='Requeued after an interrupted worker replacement.',
        started_at=None,
        completed_at=None,
    )


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0276_hospital_code_import')]

    operations = [
        migrations.RunPython(requeue_interrupted_import, migrations.RunPython.noop),
    ]
