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


def test_a_unitless_molar_hemoglobin_is_not_anemia():
    """59260-0 is moles/volume: 8 mmol/L (about 13 g/dL) read in the g/dL
    default was anemia, and every normal molar value is below 10 (#2418)."""
    person = PersonFactory()
    _measure(person, '59260-0', 8.0, '')
    assert _crab(person) is None


def test_a_row_under_the_molar_hemoglobin_code_counts_with_a_readable_unit():
    person = PersonFactory()
    _measure(person, '59260-0', 8.5, 'g/dL')
    assert _crab(person) is True


def test_a_molar_hemoglobin_in_mmol_per_litre_is_no_evidence_yet():
    """hemoglobin_units has no reviewed mmol/L factor: 5 mmol/L (about 8 g/dL)
    is not read, rather than read as 5 g/dL."""
    person = PersonFactory()
    _measure(person, '59260-0', 5.0, 'mmol/L')
    assert _crab(person) is None


def test_a_unitless_row_under_the_second_calcium_code_is_not_evidence():
    person = PersonFactory()
    _measure(person, '2000-0', 12.0, '')
    assert _crab(person) is None


def test_a_conventional_calcium_without_a_unit_takes_mg_dl():
    person = PersonFactory()
    _measure(person, '17861-6', 11.6, '')
    assert _crab(person) is True


@pytest.mark.parametrize('code, value, unit', [
    ('718-7', -1.0, 'g/dL'),        # meets every "< N" threshold
    ('718-7', 0.0, 'g/dL'),         # in the column, but a placeholder
    ('17861-6', 1200.0, 'mg/dL'),   # beyond the column, still "> 11"
])
def test_an_out_of_range_value_is_not_evidence(code, value, unit):
    person = PersonFactory()
    _measure(person, code, value, unit)
    assert _crab(person) is None


@pytest.mark.parametrize('concept_code, source', [('718-7', '59260-0'), ('59260-0', '718-7')])
def test_a_row_under_both_hemoglobin_codes_takes_the_stricter_default(concept_code, source):
    """A unitless row matching the mass code one way and the molar code the
    other is read as the molar one: no default, no evidence.

    Guards the outcome only: with the molar code listed last, the plain last
    write already gives no default, so this passes without the stricter-
    default merge in newest_canonical, which protects a reordering."""
    from tests.factories import ConceptFactory
    person = PersonFactory()
    MeasurementFactory(
        person=person, measurement_concept=ConceptFactory(concept_code=concept_code),
        measurement_source_value=source, measurement_date=NEW,
        value_as_number=8.0, unit_source_value='')
    assert _crab(person) is None
