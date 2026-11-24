from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0346_protein_expressions_record_only"),
    ]

    operations = [
        migrations.AlterField(
            model_name="patientrecord",
            name="pulmonary_function_test_result",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="patientrecord",
            name="bone_imaging_result",
            field=models.BooleanField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="patientrecord",
            name="pregnancy_test_result",
            field=models.BooleanField(
                blank=True,
                help_text=(
                    "Does the female patient of childbearing age have a negative "
                    "test result for pregnancy?"
                ),
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="patientrecord",
            name="no_concomitant_medication_status",
            field=models.BooleanField(
                blank=True,
                help_text="Does the patient have concomitant medication?",
                null=True,
            ),
        ),
    ]
