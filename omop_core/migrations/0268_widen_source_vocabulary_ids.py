from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0267_relabel_icd10_to_icd10cm'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sourcecodeconceptmapping',
            name='source_vocabulary_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                help_text=(
                    'External code system the code arrived in (ICD10CM, LOINC, SNOMED, '
                    'NDC, ...). Blank means uncoded — a paper lab test name or free '
                    'text from a note, which is legitimate and common. Never an HK-* '
                    'vocabulary: those are minting destinations, not source systems.'
                ),
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name='mappingsuggestionreview',
            name='source_vocabulary_id',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name='suggestrun',
            name='source_vocabulary_id',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
