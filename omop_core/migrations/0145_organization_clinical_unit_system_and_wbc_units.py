from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0144_concept_unique_vocabulary_code')]

    operations = [
        migrations.AddField(
            model_name='organization',
            name='clinical_unit_system',
            field=models.CharField(
                choices=[('US_ONCOLOGY', 'US oncology (mCODE/USCDI)'), ('SI', 'SI')],
                default='US_ONCOLOGY',
                help_text='Default canonical units for derived clinical compatibility fields.',
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='patientrecord',
            name='white_blood_cell_count_units',
            field=models.CharField(
                blank=True,
                choices=[
                    ('10*3/uL', '10^3/μL (US oncology)'), ('10*9/L', '10^9/L (SI)'),
                    ('CELLS/UL', 'Legacy CELLS/UL'), ('CELLS/L', 'Legacy CELLS/L'),
                ],
                default='10*3/uL', max_length=10, null=True,
            ),
        ),
    ]
