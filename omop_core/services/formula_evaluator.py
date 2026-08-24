"""
formula_evaluator.py — Safe expression validator for field formulas.

Validates formula expressions against a whitelist of allowed AST nodes.
Does NOT evaluate formulas — validation only in this phase.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from omop_core.models import PatientRecord


# Supported @-functions in formulas.
ALLOWED_FUNCTIONS = frozenset({'not', 'count', 'abs', 'min', 'max'})

# AST node types allowed in formula expressions.
_ALLOWED_NODES = (
    ast.Module, ast.Expr, ast.Expression,
    ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod, ast.FloorDiv,
    ast.USub, ast.UAdd, ast.Not,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
    ast.And, ast.Or,
    ast.Name, ast.Constant, ast.Load,
    ast.Call,
)


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]

    def __bool__(self):
        return self.valid


def _get_patient_record_fields() -> frozenset[str]:
    """Return the set of all concrete PatientRecord field names."""
    return frozenset(
        f.name for f in PatientRecord._meta.get_fields()
        if getattr(f, 'concrete', False)
    )


def _preprocess_formula(formula: str) -> str:
    """Convert @-functions to regular Python function calls for AST parsing.

    @not(x) → __fn_not(x)
    @count(x) → __fn_count(x)
    """
    return re.sub(r'@(\w+)\s*\(', r'__fn_\1(', formula)


def _replace_caret_with_pow(formula: str) -> str:
    """Replace ^ (caret) with ** (Python power operator)."""
    return formula.replace('^', '**')


def validate_formula(formula: str) -> ValidationResult:
    """Validate a formula expression.

    Returns a ValidationResult with valid=True if the formula is safe to store,
    or valid=False with a list of error messages.
    """
    errors: list[str] = []

    if not formula or not formula.strip():
        return ValidationResult(valid=False, errors=['Formula is empty.'])

    # Preprocess: convert @-functions and ^ operator.
    processed = _preprocess_formula(formula.strip())
    processed = _replace_caret_with_pow(processed)

    # Parse into AST.
    try:
        tree = ast.parse(processed, mode='eval')
    except SyntaxError as e:
        return ValidationResult(valid=False, errors=[f'Syntax error: {e.msg}'])

    # Walk AST and validate nodes.
    valid_fields = _get_patient_record_fields()
    function_names_used: list[str] = []
    field_names_used: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            errors.append(f'Disallowed expression type: {type(node).__name__}')
            continue

        if isinstance(node, ast.Name):
            name = node.id
            if name.startswith('__fn_'):
                # This is a function reference from @-function preprocessing.
                fn_name = name[5:]  # Strip __fn_ prefix.
                function_names_used.append(fn_name)
                if fn_name not in ALLOWED_FUNCTIONS:
                    errors.append(f'Unknown function: @{fn_name}')
            else:
                field_names_used.append(name)
                if name not in valid_fields:
                    errors.append(f'Unknown field: {name}')

        if isinstance(node, ast.Call):
            # Verify the callable is an allowed function.
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                if func_name.startswith('__fn_'):
                    pass  # Already validated above.
                else:
                    errors.append(f'Direct function calls not allowed: {func_name}()')
            else:
                errors.append(
                    f'Only @-function calls are allowed, not {type(node.func).__name__}'
                )

    return ValidationResult(valid=len(errors) == 0, errors=errors)
