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


def test_deactivating_a_stale_formula_is_still_allowed():
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
