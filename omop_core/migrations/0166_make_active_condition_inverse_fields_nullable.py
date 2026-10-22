"""Allow active-condition inverse projections to remain unknown."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0165_alter_patientrecord_kappa_lambda_ratio'),
    ]

    operations = [
        migrations.AlterField(
            model_name='patientrecord',
            name='no_active_infection_status',
            field=models.BooleanField(blank=True, default=None, help_text='Does the patient have any active infection?', null=True),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='no_other_active_malignancies',
            field=models.BooleanField(blank=True, default=None, null=True),
        ),
    ]
