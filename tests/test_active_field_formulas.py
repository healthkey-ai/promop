"""Execution and immediate backfill coverage for curated field formulas."""

import pytest

from omop_core.models import FieldFormula
from omop_core.services.patient_record_service import (
    _compute_derived_fields,
    recompute_formula_field,
    refresh_patient_record,
)
from tests.factories import PatientRecordFactory


pytestmark = pytest.mark.django_db


def test_active_formula_overrides_its_target_during_patient_derivation():
    record = PatientRecordFactory(weight=80, height=200)
    FieldFormula.objects.create(
        field_name='bmi', formula='weight / (height / 100) ^ 2', is_active=True,
    )

    _compute_derived_fields(record)

    assert float(record.bmi) == 20


def test_formula_edit_backfills_existing_patient_records_immediately():
    record = PatientRecordFactory(weight=90, height=180, bmi=None)
    formula = FieldFormula.objects.create(
        field_name='bmi', formula='weight / (height / 100) ^ 2', is_active=True,
    )

    assert recompute_formula_field(formula) == 1
    record.refresh_from_db()
    assert float(record.bmi) == pytest.approx(27.77777777777778)


def test_active_formula_is_final_authority_after_patient_record_save():
    record = PatientRecordFactory()
    FieldFormula.objects.create(field_name='bmi', formula='42', is_active=True)

    refreshed = refresh_patient_record(record.person)

    assert float(refreshed.bmi) == 42
