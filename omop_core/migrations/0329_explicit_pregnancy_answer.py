"""Make the pregnancy/lactation self-assessment an explicit answer (#2343).

`no_pregnancy_or_lactation_status` was seeded with the formula
`@not(pregnancy_test_result)` and kept `null=False`. A direct answer was
therefore overwritten by the next derivation, and the shared editor's
"— None —" was rejected by the API.

The question is a self-assessment, not a computed result: deactivate the seeded
formula, make the column nullable so Unknown can be stored, and refuse an
active formula for it (the same contract as #5666).
"""
from django.db import migrations, models


FIELD = 'no_pregnancy_or_lactation_status'
EXPLICIT_FIELDS = (
    'no_active_infection_status',
    'no_other_active_malignancies',
    FIELD,
)


def refuse_if_nulls(apps, schema_editor):
    """Refuse to make the column NOT NULL again while it holds Unknown answers.

    The reverse AlterField would fill every NULL with the old default (True),
    turning "not answered" into "not pregnant or lactating". Mirrors
    0328.refuse_lossy_reverse (#2489).
    """
    with schema_editor.connection.cursor() as cursor:
        # Exclude writers between this check and the reverse ALTER.
        cursor.execute('LOCK TABLE public.patient_record IN ACCESS EXCLUSIVE MODE')
        cursor.execute(f'SELECT 1 FROM patient_record WHERE {FIELD} IS NULL LIMIT 1')
        if cursor.fetchone():
            raise RuntimeError(
                f'Refusing to make {FIELD} NOT NULL: at least one row is '
                'unanswered, and the reverse would record it as True.'
            )


def deactivate_formula(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    alias = schema_editor.connection.alias if schema_editor is not None else 'default'
    FieldFormula.objects.using(alias).filter(field_name=FIELD).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0328_widen_count_precision'),
    ]

    operations = [
        migrations.RunPython(deactivate_formula, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='patientrecord',
            name=FIELD,
            field=models.BooleanField(
                blank=True, null=True, default=None,
                help_text='Does the patient self assess as not pregnant or lactating?',
            ),
        ),
        migrations.RemoveConstraint(
            model_name='fieldformula',
            name='field_formula_explicit_answer_inactive',
        ),
        migrations.AddConstraint(
            model_name='fieldformula',
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(field_name__in=EXPLICIT_FIELDS)
                    | models.Q(is_active=False)
                ),
                name='field_formula_explicit_answer_inactive',
            ),
        ),
        # Forward: nothing. This runs first on a reverse migration, before the
        # column becomes NOT NULL again.
        migrations.RunPython(migrations.RunPython.noop, refuse_if_nulls),
    ]
