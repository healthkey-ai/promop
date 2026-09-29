from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0267_relabel_icd10_to_icd10cm'),
    ]

    operations = [
        migrations.AddField(
            model_name='vocabularyrelease',
            name='source_artifact_identity',
            field=models.CharField(blank=True, db_index=True, help_text='Stable provider identity for the Athena source artifact.', max_length=500, null=True),
        ),
        migrations.AddField(
            model_name='vocabularyrelease',
            name='source_artifact_sha256',
            field=models.CharField(blank=True, db_index=True, help_text='SHA-256 of the Athena ZIP used for this release.', max_length=64, null=True),
        ),
        migrations.CreateModel(
            name='AthenaVocabularySync',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_url', models.TextField()),
                ('source_artifact_identity', models.CharField(blank=True, max_length=500)),
                ('source_artifact_sha256', models.CharField(blank=True, max_length=64)),
                ('missing_rows', models.JSONField(blank=True, default=dict)),
                ('outcome', models.CharField(choices=[('current', 'Current'), ('delta_available', 'Delta available'), ('dry_run', 'Dry run'), ('applied', 'Applied'), ('failed', 'Failed')], db_index=True, max_length=32)),
                ('failure_reason', models.TextField(blank=True)),
                ('started_at', models.DateTimeField()),
                ('completed_at', models.DateTimeField(auto_now_add=True)),
                ('installed_release', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='omop_core.vocabularyrelease')),
                ('previous_release', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='omop_core.vocabularyrelease')),
            ],
            options={
                'db_table': 'athena_vocabulary_sync',
                'ordering': ['-completed_at'],
            },
        ),
    ]
