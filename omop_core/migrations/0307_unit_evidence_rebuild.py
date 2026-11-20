import logging

from django.db import migrations, models, transaction

logger = logging.getLogger(__name__)

REASON = 'migration 0307: rebuild unit evidence from patient data on the worker (#2043)'


def request_rebuild(apps, schema_editor):
    # The seeded instances carry the inventory's label-level unit counts on
    # every code with the label (#2041). The real units are on the loaded
    # patients' source codes and stored measurements, so the worker rewrites
    # every code's evidence from them and refreshes its variants (#2043),
    # through the same durable request as 0303's walk. A plain refresh still
    # queued is folded into this one; one already running cannot be joined,
    # and `manage.py refresh_axis_variants --rebuild-evidence --queue` records
    # the rebuild once it has finished.
    AxisVariantRefresh = apps.get_model('omop_core', 'AxisVariantRefresh')
    active = AxisVariantRefresh.objects.filter(outcome__in=('queued', 'running')).order_by('pk').first()
    if active is not None:
        if active.rebuild_evidence:
            return
        # Guarded on the outcome: a worker that claimed the row meanwhile has
        # already read rebuild_evidence=False and would not perform it.
        if active.outcome == 'queued' and AxisVariantRefresh.objects.filter(pk=active.pk, outcome='queued').update(
                rebuild_evidence=True, reason=f'{active.reason}; {REASON}'):
            return
        logger.warning('Axis variant refresh %s is running; the unit evidence rebuild (#2043) was not '
                       'requested. Run `manage.py refresh_axis_variants --rebuild-evidence --queue` once it '
                       'has finished.', active.pk)
        return
    row = AxisVariantRefresh.objects.create(reason=REASON, rebuild_evidence=True)

    def publish():
        try:
            from omop_core.models import AxisVariantRefresh as LiveModel
            from omop_core.services import axis_variant_jobs

            axis_variant_jobs.publish(LiveModel.objects.get(pk=row.pk))
        except Exception:
            logger.warning('Axis variant refresh %s not published; the worker will publish it.',
                           row.pk, exc_info=True)

    transaction.on_commit(publish)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0306_merge_20261025_1010'),
    ]

    operations = [
        migrations.AddField(
            model_name='axisvariantrefresh',
            name='rebuild_evidence',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='axisvariantrefresh',
            name='cursor',
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(request_rebuild, migrations.RunPython.noop),
    ]
