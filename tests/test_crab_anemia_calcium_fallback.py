"""The meets_crab fallback reads hemoglobin and calcium in their canonical units (#2441).

It compared the raw ``value_as_number``: 8.1 mmol/L hemoglobin (about 13 g/dL)
looked like anemia, and 2.9 mmol/L calcium (about 11.6 mg/dL) looked normal.
Both had to be read across the whole history, so one old result set CRAB for
good. Creatinine gained the unit/recency handling in #2430; these are the other
two arms.
"""
from datetime import date

import pytest

from omop_core.services.patient_record_service import _get_mm_specific_data
from tests.factories import MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db

OLD, NEW = date(2020, 1, 1), date(2026, 10, 1)


def _measure(person, code, value, unit, day=NEW):
    MeasurementFactory(
        person=person,
        measurement_source_value=code,
        measurement_date=day,
        value_as_number=value,
        unit_source_value=unit,
    )


def _crab(person):
    return _get_mm_specific_data(person).get('meets_crab')


def test_a_normal_si_hemoglobin_is_not_anemia():
    person = PersonFactory()
    _measure(person, '718-7', 8.1, 'mmol/L')  # ~13 g/dL
    assert _crab(person) is None


def test_a_low_conventional_hemoglobin_meets_crab():
    person = PersonFactory()
    _measure(person, '718-7', 9.5, 'g/dL')
    assert _crab(person) is True


def test_a_normal_g_per_litre_hemoglobin_is_not_anemia():
    person = PersonFactory()
    _measure(person, '718-7', 135, 'g/L')
    assert _crab(person) is None


def test_an_old_abnormal_hemoglobin_does_not_outlive_a_newer_normal_one():
    person = PersonFactory()
    _measure(person, '718-7', 7.0, 'g/dL', day=OLD)
    _measure(person, '718-7', 13.0, 'g/dL', day=NEW)
    assert _crab(person) is None


def test_a_high_si_calcium_meets_crab():
    person = PersonFactory()
    _measure(person, '17861-6', 2.9, 'mmol/L')  # ~11.6 mg/dL
    assert _crab(person) is True


def test_a_normal_si_calcium_is_not_evidence():
    person = PersonFactory()
    _measure(person, '17861-6', 2.4, 'mmol/L')  # ~9.6 mg/dL
    assert _crab(person) is None


def test_an_unreadable_newest_unit_is_not_evidence():
    person = PersonFactory()
    _measure(person, '718-7', 7.0, 'g/dL', day=OLD)
    _measure(person, '718-7', 5.0, 'units-unknown', day=NEW)
    assert _crab(person) is None
