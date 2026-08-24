"""Seed field formulas after every referenced PatientRecord field exists."""

import ast
import re

from django.db import migrations


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

_ALLOWED_FUNCTIONS = frozenset({'not', 'count', 'abs', 'min', 'max'})
_ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv,
    ast.USub, ast.UAdd, ast.Not, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.And, ast.Or, ast.Name, ast.Constant, ast.Load,
    ast.Call,
)


def _validate_formula(formula, fields):
    """Migration-local validator using the historical PatientRecord model."""
    processed = re.sub(r'@(\w+)\s*\(', r'__fn_\1(', formula.strip()).replace('^', '**')
    try:
        tree = ast.parse(processed, mode='eval')
    except SyntaxError as exc:
        return [f'Syntax error: {exc.msg}']

    errors = []
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            errors.append(f'Disallowed expression type: {type(node).__name__}')
        elif isinstance(node, ast.Name):
            if node.id.startswith('__fn_'):
                if node.id[5:] not in _ALLOWED_FUNCTIONS:
                    errors.append(f'Unknown function: @{node.id[5:]}')
            elif node.id not in fields:
                errors.append(f'Unknown field: {node.id}')
        elif isinstance(node, ast.Call) and (
            not isinstance(node.func, ast.Name) or not node.func.id.startswith('__fn_')
        ):
            errors.append('Only @-function calls are allowed')
    return errors


def seed_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    PatientRecord = apps.get_model('omop_core', 'PatientRecord')
    fields = {field.name for field in PatientRecord._meta.get_fields() if field.concrete}
    for field_name, (formula, is_active) in INITIAL_FORMULAS.items():
        errors = _validate_formula(formula, fields)
        if errors:
            raise RuntimeError(
                f"Refusing to seed invalid formula for {field_name}: {'; '.join(errors)}"
            )
        FieldFormula.objects.get_or_create(
            field_name=field_name,
            defaults={'formula': formula, 'is_active': is_active},
        )


def reverse_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    for field_name, (formula, _) in INITIAL_FORMULAS.items():
        FieldFormula.objects.filter(field_name=field_name, formula=formula).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0166_make_active_condition_inverse_fields_nullable'),
    ]

    operations = [
        migrations.RunPython(seed_formulas, reverse_formulas),
    ]
