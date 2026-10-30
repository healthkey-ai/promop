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


def test_retired_viral_inverse_formula_does_not_create_a_field():
    record = PatientRecordFactory(hiv_status=None)
    formula, _ = FieldFormula.objects.update_or_create(
        field_name='no_hiv_status',
        defaults={'formula': '@not(hiv_status)', 'is_active': True},
    )
    recompute_formula_field(formula)
    record.refresh_from_db()
    assert record.hiv_status is None
    assert 'no_hiv_status' not in record.custom_fields
    assert not hasattr(record, 'no_hiv_status')


def test_a_deactivated_pre_existing_formula_leaves_the_flag_unknown():
    """The shipped formula is off, so an unassessed list stays unknown (#1988)."""
    record = PatientRecordFactory(preexisting_conditions=[], no_pre_existing_conditions=None)
    FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=False,
    )

    _compute_derived_fields(record)

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None


def test_the_same_formula_active_reproduces_the_defect():
    """Guards the regression above: the expression it replaced did assert "none"."""
    record = PatientRecordFactory(preexisting_conditions=[], no_pre_existing_conditions=None)
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=True,
    )

    assert recompute_formula_field(formula) >= 1

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is True


def test_the_shipped_migration_deactivates_the_formula():
    from importlib import import_module

    from django.apps import apps as django_apps

    migration = import_module(
        'omop_core.migrations.0304_deactivate_pre_existing_condition_formula')
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=True,
    )

    migration.deactivate(django_apps, None)

    formula.refresh_from_db()
    assert formula.is_active is False


def test_the_shipped_curation_keeps_the_formula_inactive():
    """A curation re-import must not re-enable it (#1988)."""
    import json
    import pathlib

    fixture = pathlib.Path(__file__).resolve().parents[1] / 'omop_core' / 'data' / 'field_curation_v1.json'
    data = json.loads(fixture.read_text())

    def formulas(node):
        if isinstance(node, dict):
            if node.get('field_name') == 'no_pre_existing_conditions' and 'formula' in node:
                yield node
            for value in node.values():
                yield from formulas(value)
        elif isinstance(node, list):
            for value in node:
                yield from formulas(value)

    shipped = list(formulas(data))
    assert shipped, 'the curation fixture no longer carries the pre-existing-conditions formula'
    assert all(entry['is_active'] is False for entry in shipped)
