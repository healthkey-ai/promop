# Generated manually: runtime PatientRecord fields use a JSON projection rather
# than per-field schema migrations.

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0173_backfill_blood_count_units'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='patientrecord',
            name='custom_fields',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.CreateModel(
            name='CustomPatientField',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('field_name', models.CharField(db_index=True, max_length=100, unique=True)),
                ('display_name', models.CharField(max_length=200)),
                ('tab', models.CharField(choices=[('general', 'General'), ('disease', 'Disease'), ('treatment', 'Treatment'), ('blood', 'Blood'), ('labs', 'Labs'), ('behavior', 'Behavior'), ('wearable', 'Wearable')], max_length=20)),
                ('field_type', models.CharField(choices=[('text', 'Text'), ('number', 'Number'), ('date', 'Date'), ('boolean', 'Boolean')], max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('mapping', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='custom_patient_field', to='omop_core.fieldconceptmapping')),
            ],
            options={'db_table': 'custom_patient_field', 'ordering': ['tab', 'display_name', 'field_name']},
        ),
    ]
