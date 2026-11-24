from django.db import migrations, models

FIELDS = (
    'pulmonary_function_test_result',
    'bone_imaging_result',
    'pregnancy_test_result',
    'no_concomitant_medication_status',
)


def refuse_if_nulls(apps, schema_editor):
    """Refuse to restore NOT NULL while a row holds an Unknown answer.

    The reverse AlterField would fill every NULL with the old default (False for
    the tests, True for no concomitant medication), turning "not answered" into
    a clinical assertion. Mirrors 0329.refuse_if_nulls.
    """
    with schema_editor.connection.cursor() as cursor:
        cursor.execute('LOCK TABLE public.patient_record IN ACCESS EXCLUSIVE MODE')
        for field in FIELDS:
            cursor.execute(
                f'SELECT 1 FROM patient_record WHERE {field} IS NULL LIMIT 1')
            if cursor.fetchone():
                raise RuntimeError(
                    f'Refusing to make {field} NOT NULL: at least one row is '
                    'unanswered, and the reverse would record an answer nobody gave.'
                )


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
        # Forward: nothing. Runs first on a reverse migration, before the
        # columns become NOT NULL again.
        migrations.RunPython(migrations.RunPython.noop, refuse_if_nulls),
    ]
