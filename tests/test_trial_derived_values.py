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
