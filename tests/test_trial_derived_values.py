"""Derived values Trial Details reads without a column.

`abnormal_kappa_lambda_ratio` and `day_since_stem_cell_transplant` are answers a
trial asks for, computed from facts the patient already records. They are not
columns: the form offers the sources, never a second place to type the verdict.
"""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from omop_core.models import PatientRecord
from omop_core.services.patient_form import patient_form_descriptors

pytestmark = pytest.mark.django_db


def _record(**values):
    return PatientRecord(**values)


@pytest.mark.parametrize(
    ('kappa', 'lam', 'expected'),
    [
        (Decimal('0.26'), Decimal('1'), False),   # inclusive low boundary
        (Decimal('1.65'), Decimal('1'), False),   # inclusive high boundary
        (Decimal('0.25'), Decimal('1'), True),
        (Decimal('1.66'), Decimal('1'), True),
        (Decimal('2'), Decimal('1'), True),
        (None, Decimal('1'), None),
        (Decimal('2'), None, None),
        (Decimal('2'), Decimal('0'), None),
        (Decimal('NaN'), Decimal('1'), None),
        (Decimal('Infinity'), Decimal('1'), None),
        (Decimal('1'), Decimal('NaN'), None),
        (Decimal('1'), Decimal('Infinity'), None),
        # Invalid strings are unknown, not a crash.
        ('not-a-number', Decimal('1'), None),
        (Decimal('1'), '', None),
        # Floats go through the same Decimal split.
        (2.0, 1.0, True),
        (-1, -2, False),  # ratio 0.5, still inside the window
    ],
)
def test_the_flc_window_is_inclusive_and_unknown_stays_unknown(kappa, lam, expected):
    assert _record(kappa_flc=kappa, lambda_flc=lam).abnormal_kappa_lambda_ratio is expected


def test_a_boundary_ratio_survives_the_decimal_split():
    """1.65 must not land outside the window through binary-float error."""
    assert _record(kappa_flc=Decimal('100.65'),
                   lambda_flc=Decimal('61')).abnormal_kappa_lambda_ratio is False


def test_days_since_transplant_counts_completed_days():
    record = _record(sct_date=date.today() - timedelta(days=30))
    assert record.day_since_stem_cell_transplant == 30


def test_today_is_day_zero():
    assert _record(sct_date=date.today()).day_since_stem_cell_transplant == 0


@pytest.mark.parametrize('sct_date', [None, date.today() + timedelta(days=1)])
def test_a_missing_or_future_transplant_is_unknown(sct_date):
    assert _record(sct_date=sct_date).day_since_stem_cell_transplant is None


def test_an_assigned_datetime_is_normalized_before_the_comparison():
    from datetime import datetime

    record = _record(sct_date=datetime.combine(
        date.today() - timedelta(days=7), datetime.min.time()))
    assert record.day_since_stem_cell_transplant == 7


@pytest.mark.parametrize(
    ('name', 'inputs'),
    [
        ('abnormal_kappa_lambda_ratio', ['kappa_flc', 'lambda_flc']),
        ('day_since_stem_cell_transplant', ['sct_date']),
    ],
)
def test_the_form_navigates_to_the_sources_instead_of_the_verdict(name, inputs):
    fields = patient_form_descriptors(readable_fields=set(), writable=True)

    entry = fields[name]
    assert entry['kind'] == 'computed'
    assert entry['writable'] is False
    assert entry['inputs'] == inputs


def test_the_verdicts_are_not_writable_fields():
    """Neither property is a column, so the fact writer must refuse both."""
    from omop_core.services.field_write_service import apply_field_writes
    from tests.factories import PatientRecordFactory, PersonFactory

    person = PersonFactory()
    PatientRecordFactory(person=person)
    result = apply_field_writes(person, {
        'abnormal_kappa_lambda_ratio': True,
        'day_since_stem_cell_transplant': 30,
    }, today=date.today())

    assert result.applied == []
    assert set(result.rejected) == {
        'abnormal_kappa_lambda_ratio', 'day_since_stem_cell_transplant',
    }


@pytest.mark.parametrize(
    ('name', 'true_label', 'false_label'),
    [
        ('pregnancy_test_result', 'Negative', 'Not negative'),
        ('no_pregnancy_or_lactation_status', 'Not pregnant or lactating', 'Pregnant or lactating'),
        ('plasma_cell_leukemia', 'Present', 'Not present'),
    ],
)
def test_the_answer_words_keep_the_question_polarity(name, true_label, false_label):
    """A bare Yes/No on a negative question reads as the opposite answer."""
    fields = patient_form_descriptors(readable_fields={name}, writable=True)

    assert fields[name]['answer_labels'] == {'true': true_label, 'false': false_label}


def test_age_navigates_to_the_birth_date_it_is_computed_from():
    """#5664: the age is shown; the only edit is the date, with its precision."""
    fields = patient_form_descriptors(readable_fields={'patient_age', 'date_of_birth'},
                                     writable=True)

    age = fields['patient_age']
    assert age['kind'] == 'computed'
    assert age['writable'] is False
    assert age['inputs'] == ['date_of_birth']
    assert fields['date_of_birth']['writable'] is True
