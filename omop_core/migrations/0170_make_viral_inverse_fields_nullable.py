"""Allow unknown viral-status inverse projections."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0169_alter_active_malignancies_default'),
    ]

    operations = [
        migrations.AlterField(
            model_name='patientrecord',
            name='no_hiv_status',
            field=models.BooleanField(blank=True, default=None, help_text='Does the patient has had HIV?', null=True),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='no_hepatitis_b_status',
            field=models.BooleanField(blank=True, default=None, help_text='Does the patient has had Hepatitis B (HBV)?', null=True),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='no_hepatitis_c_status',
            field=models.BooleanField(blank=True, default=None, help_text='Does the patient has had Hepatitis C (HCV)?', null=True),
        ),
    ]
