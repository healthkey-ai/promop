from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0272_sourcecodeconceptmapping_suggested_action'),
    ]

    operations = [
        migrations.AlterField(
            model_name='athenavocabularysync',
            name='completed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='athenavocabularysync',
            name='outcome',
            field=models.CharField(
                choices=[
                    ('queued', 'Queued'),
                    ('running', 'Running'),
                    ('current', 'Current'),
                    ('delta_available', 'Delta available'),
                    ('dry_run', 'Dry run'),
                    ('applied', 'Applied'),
                    ('failed', 'Failed'),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='athenavocabularysync',
            name='task_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddConstraint(
            model_name='athenavocabularysync',
            constraint=models.UniqueConstraint(
                condition=Q(outcome__in=('queued', 'running')),
                fields=('source_url',),
                name='uq_athena_sync_active_source',
            ),
        ),
    ]
