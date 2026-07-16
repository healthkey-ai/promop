from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0109_remove_patientrecord_best_response'),
    ]

    operations = [
        migrations.AddField(
            model_name='patientrecord',
            name='death_date',
            field=models.DateField(
                blank=True,
                help_text='Date of death (from OMOP Death)',
                null=True,
            ),
        ),
    ]
