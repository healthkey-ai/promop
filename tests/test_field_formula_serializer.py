"""A partial formula update must not re-enable a dead target (#1988)."""

import pytest

from omop_core.models import FieldFormula
from patient_portal.api.serializers import FieldFormulaSerializer

pytestmark = pytest.mark.django_db


def test_reactivating_a_formula_for_a_non_computed_field_is_refused():
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=False,
    )

    serializer = FieldFormulaSerializer(formula, data={'is_active': True}, partial=True)

    assert not serializer.is_valid()
    assert 'field_name' in serializer.errors


def test_deactivating_a_stale_formula_is_still_allowed(stale_answer_formula_allowed):
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=True,
    )

    serializer = FieldFormulaSerializer(formula, data={'is_active': False}, partial=True)

    assert serializer.is_valid(), serializer.errors


def test_a_computed_field_can_still_be_activated():
    formula = FieldFormula.objects.create(
        field_name='bmi', formula='weight / (height / 100) ^ 2', is_active=False,
    )

    serializer = FieldFormulaSerializer(formula, data={'is_active': True}, partial=True)

    assert serializer.is_valid(), serializer.errors


@pytest.mark.parametrize('field_name', [
    'no_pregnancy_or_lactation_status', 'no_active_infection_status', 'no_other_active_malignancies',
])
def test_every_field_the_check_covers_is_a_400_not_a_500(field_name):
    """field_formula_explicit_answer_inactive covers three fields; the
    serializer refused only the two derived negatives, so pregnancy reached
    the database and raised IntegrityError."""
    formula = FieldFormula.objects.create(field_name=field_name, formula='true', is_active=False)

    serializer = FieldFormulaSerializer(formula, data={'is_active': True}, partial=True)

    assert not serializer.is_valid()
    assert 'field_name' in serializer.errors


def test_editing_an_inactive_formula_is_not_an_activation():
    """A PATCH without is_active kept the stored False, but validate read the
    missing key as an activation and refused the edit (#2489)."""
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=False,
    )

    serializer = FieldFormulaSerializer(
        formula, data={'formula': '@count(preexisting_conditions) == 1'}, partial=True)

    assert serializer.is_valid(), serializer.errors
    assert serializer.save().is_active is False


def test_editing_an_active_formula_still_checks_its_target(stale_answer_formula_allowed):
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions', formula='true', is_active=True,
    )

    serializer = FieldFormulaSerializer(
        formula, data={'formula': '@count(preexisting_conditions) == 1'}, partial=True)

    assert not serializer.is_valid()
    assert 'field_name' in serializer.errors
