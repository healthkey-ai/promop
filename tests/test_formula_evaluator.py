"""Tests for the formula evaluator service."""

import pytest

from omop_core.services.formula_evaluator import validate_formula, ALLOWED_FUNCTIONS


pytestmark = pytest.mark.django_db


def test_valid_negation_formula():
    """@not() formulas are valid."""
    result = validate_formula('@not(hiv_status)')
    assert result.valid, f"Expected valid but got errors: {result.errors}"


def test_valid_count_formula():
    """@count() formulas with comparisons are valid."""
    result = validate_formula('@count(preexisting_conditions) == 0')
    assert result.valid, f"Expected valid but got errors: {result.errors}"


def test_valid_arithmetic_formula():
    """Arithmetic expressions with field references are valid."""
    result = validate_formula('weight / (height / 100) ^ 2')
    assert result.valid, f"Expected valid but got errors: {result.errors}"


def test_valid_division_formula():
    """The involved/uninvolved formula is valid."""
    result = validate_formula('@max(kappa_flc, lambda_flc) / @min(kappa_flc, lambda_flc)')
    assert result.valid, f"Expected valid but got errors: {result.errors}"


def test_invalid_field_reference():
    """Unknown field names are rejected."""
    result = validate_formula('nonexistent_field + 1')
    assert not result.valid
    assert any('Unknown field' in e for e in result.errors)


def test_unknown_function_rejected():
    """Unknown @-functions are rejected."""
    result = validate_formula('@eval(something)')
    assert not result.valid
    assert any('Unknown function' in e for e in result.errors)


def test_empty_formula_rejected():
    """Empty formulas are rejected."""
    result = validate_formula('')
    assert not result.valid
    assert any('empty' in e.lower() for e in result.errors)


def test_syntax_error_rejected():
    """Syntactically invalid formulas are rejected."""
    result = validate_formula('weight / / height')
    assert not result.valid
    assert any('Syntax error' in e for e in result.errors)


def test_allowed_functions_list():
    """ALLOWED_FUNCTIONS contains the expected functions."""
    assert 'not' in ALLOWED_FUNCTIONS
    assert 'count' in ALLOWED_FUNCTIONS
    assert 'abs' in ALLOWED_FUNCTIONS
    assert 'min' in ALLOWED_FUNCTIONS
    assert 'max' in ALLOWED_FUNCTIONS


def test_constant_expression():
    """Pure constant expression is valid (though pointless)."""
    result = validate_formula('42')
    assert result.valid


def test_comparison_expression():
    """Comparison with field is valid."""
    result = validate_formula('weight > 50')
    assert result.valid, f"Expected valid but got errors: {result.errors}"
