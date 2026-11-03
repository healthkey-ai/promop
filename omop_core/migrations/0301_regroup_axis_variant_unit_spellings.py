from django.db import migrations


class Migration(migrations.Migration):
    # #1966 re-ran the axis variant refresh here so stored variants regrouped
    # under the corrected unit spellings. Like 0300 it could not finish inside
    # the Cloud Run migrate job on the HealthTree instance (#2021); the worker
    # job that migration 0303 requests regroups them instead.
    atomic = False

    dependencies = [
        ('omop_core', '0300_backfill_axis_variants'),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
