"""Seed initial field choices (value sets) and formulas for computed fields."""

from django.db import migrations


# field_name → [(display, [(code, vocabulary_id, display, is_primary), ...])]
INITIAL_CHOICES = {
    'disease': [
        ('Follicular Lymphoma', [('307618003', 'SNOMED', 'Follicular lymphoma', True), ('C82', 'ICD10CM', 'Follicular lymphoma', False)]),
        ('Multiple Myeloma', [('109989006', 'SNOMED', 'Multiple myeloma', True), ('C90.0', 'ICD10CM', 'Multiple myeloma', False)]),
        ('Breast Cancer', [('254837009', 'SNOMED', 'Malignant neoplasm of breast', True), ('C50', 'ICD10CM', 'Malignant neoplasm of breast', False)]),
        ('Chronic Lymphocytic Leukemia', [('92814006', 'SNOMED', 'Chronic lymphocytic leukemia', True), ('C91.1', 'ICD10CM', 'Chronic lymphocytic leukemia', False)]),
        ('Mantle Cell Lymphoma', [('307621008', 'SNOMED', 'Mantle cell lymphoma', True), ('C85.7', 'ICD10CM', 'Other specified types of non-Hodgkin lymphoma', False)]),
    ],
    'smoking_status': [
        ('Current smoker', [('77176002', 'SNOMED', 'Smoker', True)]),
        ('Former smoker', [('8517006', 'SNOMED', 'Ex-smoker', True)]),
        ('Never smoker', [('266919005', 'SNOMED', 'Never smoked', True)]),
        ('Unknown', [('266927001', 'SNOMED', 'Tobacco use and exposure - finding', True)]),
    ],
    'alcohol_use': [
        ('Current drinker', [('219006', 'SNOMED', 'Current drinker of alcohol', True)]),
        ('Former drinker', [('82581004', 'SNOMED', 'Ex-drinker', True)]),
        ('Non-drinker', [('105542008', 'SNOMED', 'Non-drinker', True)]),
    ],
    'menopausal_status': [
        ('Pre-menopausal', [('309606002', 'SNOMED', 'Pre-menopausal', True)]),
        ('Peri-menopausal', [('277393006', 'SNOMED', 'Peri-menopausal', True)]),
        ('Post-menopausal', [('76498008', 'SNOMED', 'Post-menopausal', True)]),
    ],
    'exercise_frequency': [
        ('Sedentary', [('160726008', 'SNOMED', 'Sedentary lifestyle', True)]),
        ('Light exercise', [('228453002', 'SNOMED', 'Light exercise', True)]),
        ('Moderate exercise', [('228454008', 'SNOMED', 'Moderate exercise', True)]),
        ('Vigorous exercise', [('228455009', 'SNOMED', 'Vigorous exercise', True)]),
    ],
    'insurance_status': [
        ('Insured', [('160701007', 'SNOMED', 'Health insurance', True)]),
        ('Uninsured', [('160702000', 'SNOMED', 'No health insurance', True)]),
        ('Underinsured', []),
    ],
    'employment_status': [
        ('Employed', [('224362002', 'SNOMED', 'Employed', True)]),
        ('Unemployed', [('73438004', 'SNOMED', 'Unemployed', True)]),
        ('Retired', [('105493001', 'SNOMED', 'Retired', True)]),
        ('Disabled', [('105502003', 'SNOMED', 'Disabled', True)]),
    ],
}

# field_name → (formula, is_active)
INITIAL_FORMULAS = {
    'no_active_infection_status': ('@not(active_infection_status)', False),
    'no_hiv_status': ('@not(hiv_status)', False),
    'no_hepatitis_b_status': ('@not(hepatitis_b_status)', False),
    'no_hepatitis_c_status': ('@not(hepatitis_c_status)', False),
    'no_other_active_malignancies': ('@count(active_malignancies) <= 1', False),
    'no_pre_existing_conditions': ('@count(preexisting_conditions) == 0', False),
    'no_pregnancy_or_lactation_status': ('@not(pregnancy_test_result)', False),
    'bmi': ('weight / (height / 100) ^ 2', False),
    'involved_uninvolved_ratio': ('@max(kappa_flc, lambda_flc) / @min(kappa_flc, lambda_flc)', False),
}


def seed_choices(apps, schema_editor):
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    for field_name, entries in INITIAL_CHOICES.items():
        for sort_order, (display, codes) in enumerate(entries):
            choice, _ = FieldChoice.objects.get_or_create(
                field_name=field_name,
                display=display,
                defaults={'sort_order': sort_order},
            )
            for code, vocab, code_display, is_primary in codes:
                FieldChoiceCode.objects.get_or_create(
                    choice=choice,
                    vocabulary_id=vocab,
                    defaults={
                        'code': code,
                        'display': code_display,
                        'is_primary': is_primary,
                    },
                )


def seed_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    from omop_core.services.formula_evaluator import validate_formula
    for field_name, (formula, is_active) in INITIAL_FORMULAS.items():
        result = validate_formula(formula)
        if not result.valid:
            raise RuntimeError(
                f"Refusing to seed invalid formula for {field_name}: {', '.join(result.errors)}"
            )
        FieldFormula.objects.get_or_create(
            field_name=field_name,
            defaults={'formula': formula, 'is_active': is_active},
        )


def reverse_choices(apps, schema_editor):
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    for field_name, entries in INITIAL_CHOICES.items():
        displays = [display for display, _ in entries]
        FieldChoice.objects.filter(field_name=field_name, display__in=displays).delete()


def reverse_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    for field_name, (formula, _) in INITIAL_FORMULAS.items():
        FieldFormula.objects.filter(field_name=field_name, formula=formula).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0161_fieldchoice_fieldchoicecode_fieldformula'),
    ]

    operations = [
        migrations.RunPython(seed_choices, reverse_choices),
        migrations.RunPython(seed_formulas, reverse_formulas),
    ]
