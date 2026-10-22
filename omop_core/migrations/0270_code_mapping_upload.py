from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0269_athena_vocabulary_sync'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='CodeMappingUpload',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('source_vocabulary_id', models.CharField(max_length=255)),
                ('provenance', models.CharField(max_length=50)),
                ('filename', models.CharField(max_length=255)),
                ('content_sha256', models.CharField(max_length=64)),
                ('total_rows', models.PositiveIntegerField(default=0)),
                ('inserted_rows', models.PositiveIntegerField(default=0)),
                ('updated_rows', models.PositiveIntegerField(default=0)),
                ('unchanged_rows', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('actor', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'code_mapping_upload',
                'ordering': ['-created_at'],
                'constraints': [
                    models.UniqueConstraint(
                        fields=('source_vocabulary_id', 'provenance', 'content_sha256'),
                        name='uq_code_mapping_upload_artifact',
                    ),
                ],
            },
        ),
    ]
