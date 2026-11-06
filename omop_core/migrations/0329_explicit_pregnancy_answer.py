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
    ]
