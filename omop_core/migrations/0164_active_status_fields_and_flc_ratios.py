"""Add formula inputs and distinguish measured from computed FLC ratios."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0163_alter_fieldchoicecode_unique_together'),
    ]

    operations = [
        migrations.AddField(
            model_name='patientrecord',
            name='active_infection_status',
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='active_malignancies',
            field=models.JSONField(blank=True, default=list, help_text='List of currently active malignancies', null=True),
        ),
        migrations.RenameField(
            model_name='patientrecord',
            old_name='free_light_chain_ratio',
            new_name='kappa_lambda_ratio',
        ),
        migrations.AddField(
            model_name='patientrecord',
            name='involved_uninvolved_ratio',
            field=models.DecimalField(blank=True, decimal_places=3, help_text='Computed involved/uninvolved free light chain ratio', max_digits=12, null=True),
        ),
    ]
