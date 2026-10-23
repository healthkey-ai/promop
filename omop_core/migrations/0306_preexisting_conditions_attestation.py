from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0305_patientrecord_pre_existing_flag_guard'),
    ]

    operations = [
        migrations.CreateModel(
            name='PreexistingConditionsAttestation',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True,
                                           serialize=False, verbose_name='ID')),
                ('confirmed_at', models.DateTimeField()),
                ('source', models.CharField(default='patient', max_length=20)),
                ('person', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='preexisting_conditions_attestation',
                    to='omop_core.person')),
            ],
            options={
                'db_table': 'preexisting_conditions_attestation',
            },
        ),
    ]
