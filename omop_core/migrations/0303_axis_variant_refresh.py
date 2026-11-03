import logging

from django.db import migrations, models, transaction

logger = logging.getLogger(__name__)

REASON = 'migration 0303: backfill LOINC axis variants on the worker (#2021)'


def request_refresh(apps, schema_editor):
    # Migrations 0300 and 0301 ran this refresh inside migrate; on the
    # HealthTree instance it outlived the Cloud Run migrate job's hour on every
    # attempt (#2021). The row is the durable request and the worker performs
    # it (omop_core.services.axis_variant_jobs). Publishing waits for the
    # commit: a worker that consumed the message first would find no row. It
    # is best effort as well: a worker from before this task existed drops the
    # message, and the sweep a new worker runs on start publishes whatever is
    # still queued.
    AxisVariantRefresh = apps.get_model('omop_core', 'AxisVariantRefresh')
    if AxisVariantRefresh.objects.filter(outcome__in=('queued', 'running')).exists():
        return
    row = AxisVariantRefresh.objects.create(reason=REASON)

    def publish():
        try:
            from omop_core.models import AxisVariantRefresh as LiveModel
            from omop_core.services import axis_variant_jobs

            axis_variant_jobs.publish(LiveModel.objects.get(pk=row.pk))
        except Exception:
            # The live model can be ahead of this schema when a deployment
            # migrates through several releases at once. The row is what matters.
            logger.warning('Axis variant refresh %s not published; the worker will publish it.',
                           row.pk, exc_info=True)

    transaction.on_commit(publish)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0302_relevance_signal'),
    ]

    operations = [
        migrations.CreateModel(
            name='AxisVariantRefresh',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('reason', models.TextField(blank=True)),
                ('outcome', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('done', 'Done'), ('failed', 'Failed')], db_index=True, default='queued', max_length=16)),
                ('task_id', models.CharField(blank=True, max_length=255)),
                ('keys', models.PositiveIntegerField(default=0)),
                ('done', models.PositiveIntegerField(default=0)),
                ('refreshed', models.PositiveIntegerField(default=0)),
                ('failure_reason', models.TextField(blank=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'db_table': 'axis_variant_refresh',
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddConstraint(
            model_name='axisvariantrefresh',
            constraint=models.UniqueConstraint(
                condition=models.Q(('outcome__in', ('queued', 'running'))),
                fields=('outcome',), name='uq_axis_variant_refresh_active',
            ),
        ),
        migrations.RunPython(request_refresh, migrations.RunPython.noop),
    ]
