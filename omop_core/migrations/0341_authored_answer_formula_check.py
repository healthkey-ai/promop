"""Cover every authored-answer field in the explicit-answer CHECK (#2489).

`field_formula_explicit_answer_inactive` (0329) listed three fields, while
`AUTHORED_ANSWER_FORMULA_FIELDS` also names `no_pre_existing_conditions`, so
the database still accepted an active formula for it. The model now builds the
constraint from that set; this migration brings the database in step.

0304 deactivated the seeded formula for the flag. Any row activated since is
deactivated first, exactly as 0329 did for pregnancy, so the constraint cannot
fail on it.
"""
from django.conf import settings
from django.db import migrations, models

FIELD = 'no_pre_existing_conditions'


def deactivate_formula(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    alias = schema_editor.connection.alias
    FieldFormula.objects.using(alias).filter(
        field_name=FIELD, is_active=True).update(is_active=False)


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0340_merge_20261111_2124"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(deactivate_formula, migrations.RunPython.noop),
        migrations.RemoveConstraint(
            model_name="fieldformula",
            name="field_formula_explicit_answer_inactive",
        ),
        migrations.AddConstraint(
            model_name="fieldformula",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        (
                            "field_name__in",
                            (
                                "no_active_infection_status",
                                "no_other_active_malignancies",
                                "no_pre_existing_conditions",
                                "no_pregnancy_or_lactation_status",
                            ),
                        ),
                        _negated=True,
                    ),
                    ("is_active", False),
                    _connector="OR",
                ),
                name="field_formula_explicit_answer_inactive",
            ),
        ),
    ]
