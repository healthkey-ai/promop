"""Authored answers are never formula outputs (#1988, #5779 review).

A formula cannot tell an empty list from "never assessed". If an import or a
stale active row reactivates the pre-existing-conditions formula, absence of
evidence becomes a confirmed negative — a safety-relevant exclusion. The same
guard covers the derived negatives and the pregnancy self-assessment.
"""
import pytest

from omop_core.models import FieldFormula
from omop_core.services.field_curation_transfer import apply_payload
from omop_core.services.patient_record_service import _apply_active_field_formulas
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


def test_an_import_cannot_reactivate_an_authored_answer_formula():
    payload = {
        'formulas': [{
            'field_name': 'no_pre_existing_conditions',
            'formula': '@count(preexisting_conditions) == 0',
            'is_active': True,
        }],
    }

    stats = apply_payload(payload, tables=('formulas',))

    formula = FieldFormula.objects.get(field_name='no_pre_existing_conditions')
    assert formula.is_active is False
    assert any('authored-answer' in warning for warning in stats.warnings)


def test_the_evaluator_skips_a_stale_active_answer_formula():
    record = PatientRecordFactory()
    record.no_pre_existing_conditions = None
    record.preexisting_conditions = []
    record.save()
    FieldFormula.objects.create(
        field_name='no_pre_existing_conditions',
        formula='@count(preexisting_conditions) == 0',
        is_active=True,
    )

    changed = _apply_active_field_formulas(record)

    assert 'no_pre_existing_conditions' not in changed
    record.refresh_from_db()
    assert record.no_pre_existing_conditions is None


def test_the_guard_covers_the_other_authored_answers():
    from omop_core.services.authored_answers import AUTHORED_ANSWER_FORMULA_FIELDS

    assert {
        'no_pre_existing_conditions',
        'no_active_infection_status',
        'no_other_active_malignancies',
        'no_pregnancy_or_lactation_status',
    } <= set(AUTHORED_ANSWER_FORMULA_FIELDS)
