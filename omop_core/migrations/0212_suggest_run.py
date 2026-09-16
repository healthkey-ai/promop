"""Add SuggestRun, the progress record for a queued Suggest job.

The AlterField on suggestion_model_version is not part of that: `db_index=True`
was added to the model on dev without a migration, so `makemigrations --check`
was already dirty before this branch. Django emits it with the next migration
touching the app; carrying it here is what makes the check clean again.
"""

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0211_backfill_versioned_suggestion_targets'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='sourcecodeconceptmapping',
            name='last_suggest_attempt',
            field=models.CharField(blank=True, db_index=True, default='', help_text='Suggestion model version that last examined this code, whether or not it proposed anything.', max_length=20),
        ),
        migrations.AlterField(
            model_name='sourcecodeconceptmapping',
            name='suggestion_model_version',
            field=models.CharField(blank=True, db_index=True, default='', help_text='Immutable version of the suggestion model that produced this proposal (for example v0.2).', max_length=20),
        ),
        migrations.CreateModel(
            name='SuggestRun',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('source_vocabulary_id', models.CharField(blank=True, max_length=50, null=True)),
                ('state', models.CharField(choices=[('queued', 'Queued'), ('running', 'Running'), ('success', 'Success'), ('failure', 'Failure')], default='queued', max_length=10)),
                ('total', models.IntegerField(default=0)),
                ('retrieved', models.IntegerField(default=0)),
                ('done', models.IntegerField(default=0)),
                ('destinations', models.IntegerField(default=0)),
                ('remaining', models.IntegerField(default=0)),
                ('strategy_counts', models.JSONField(blank=True, default=dict)),
                ('landed_in', models.JSONField(blank=True, default=dict)),
                ('model_version', models.CharField(blank=True, default='', max_length=20)),
                ('error', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'suggest_run',
                'ordering': ['-created_at'],
            },
        ),
    ]
