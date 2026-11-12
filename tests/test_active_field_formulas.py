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


def test_an_active_answer_formula_cannot_reproduce_the_defect(stale_answer_formula_allowed):
    """The evaluator skips authored answers even when a row is left active.

    The database constraint and the import guard also refuse it, but an older
    schema or a stale object must not turn "never assessed" into "none".
    """
    record = PatientRecordFactory(preexisting_conditions=[], no_pre_existing_conditions=None)
    formula = FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=True,
    )

    assert recompute_formula_field(formula) == 0

    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None


def test_the_shipped_migration_deactivates_the_formula(stale_answer_formula_allowed):
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


def test_0341_deactivates_the_flag_formula_before_its_check(stale_answer_formula_allowed):
    """0341 adds the flag to the explicit-answer CHECK; an active row left
    since 0304 would make AddConstraint fail, so its data step deactivates it
    first and leaves other formulas alone (#2489)."""
    from importlib import import_module

    from django.apps import apps as django_apps
    from django.db import connection, migrations

    migration = import_module('omop_core.migrations.0341_authored_answer_formula_check')
    FieldFormula.objects.create(
        field_name='no_pre_existing_conditions', formula='1', is_active=True)
    FieldFormula.objects.create(field_name='bmi', formula='1', is_active=True)
    add = next(op for op in migration.Migration.operations
               if isinstance(op, migrations.AddConstraint))
    with connection.cursor() as cursor:
        # The rows' deferred FK checks must fire before the ALTER TABLE.
        cursor.execute('SET CONSTRAINTS ALL IMMEDIATE')

    with connection.schema_editor(atomic=False) as editor:
        migration.deactivate_formula(django_apps, editor)
        editor.add_constraint(FieldFormula, add.constraint)

    assert FieldFormula.objects.get(field_name='no_pre_existing_conditions').is_active is False
    assert FieldFormula.objects.get(field_name='bmi').is_active is True

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
