from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0109_remove_patientrecord_best_response'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql=(
                        'ALTER TABLE patient_record '
                        'ADD COLUMN IF NOT EXISTS death_date date NULL'
                    ),
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name='patientrecord',
                    name='death_date',
                    field=models.DateField(
                        blank=True,
                        help_text='Date of death (from OMOP Death)',
                        null=True,
                    ),
                ),
            ],
        ),
    ]
