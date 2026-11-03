from django.db import migrations


class Migration(migrations.Migration):
    # #1957 ran axis_variants.refresh_all() here, inside migrate. On the
    # HealthTree instance that outlived the Cloud Run migrate job's one-hour
    # task limit on every attempt, and no deploy reached traffic for three days
    # (#2021). The refresh is now a worker job that migration 0303 requests;
    # this migration stays so that histories which already applied it agree
    # with those that never could.
    atomic = False

    dependencies = [
        ('omop_core', '0299_loinc_axes_and_variants'),
    ]

    operations = [
        migrations.RunPython(migrations.RunPython.noop, migrations.RunPython.noop),
    ]
