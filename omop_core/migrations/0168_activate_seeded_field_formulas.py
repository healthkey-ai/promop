"""Activate the reviewed formulas seeded by the preceding migration."""

from django.db import migrations


SEEDED_FORMULAS = {
    'no_active_infection_status': '@not(active_infection_status)',
    'no_hiv_status': '@not(hiv_status)',
    'no_hepatitis_b_status': '@not(hepatitis_b_status)',
    'no_hepatitis_c_status': '@not(hepatitis_c_status)',
    'no_other_active_malignancies': '@count(active_malignancies) <= 1',
    'no_pre_existing_conditions': '@count(preexisting_conditions) == 0',
    'no_pregnancy_or_lactation_status': '@not(pregnancy_test_result)',
    'bmi': 'weight / (height / 100) ^ 2',
    'involved_uninvolved_ratio': '@max(kappa_flc, lambda_flc) / @min(kappa_flc, lambda_flc)',
}


def activate_seeded_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    for field_name, formula in SEEDED_FORMULAS.items():
        FieldFormula.objects.filter(field_name=field_name, formula=formula).update(is_active=True)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0167_seed_validated_field_formulas'),
    ]

    operations = [migrations.RunPython(activate_seeded_formulas, migrations.RunPython.noop)]
