"""The meets_crab fallback's creatinine branch reads the canonical creatinine (#2430).

It used max() over every raw 2160-0 value: no unit (80 umol/L is normal, not
"> 2") and no recency (one abnormal value years ago set CRAB for good).
"""
from datetime import date

import pytest

from omop_core.services.patient_record_service import _get_mm_specific_data
from tests.factories import MeasurementFactory, PersonFactory

pytestmark = pytest.mark.django_db

OLD, NEW = date(2020, 1, 1), date(2026, 10, 1)


def _creatinine(person, value, unit, day=NEW):
    MeasurementFactory(person=person, measurement_source_value='2160-0', measurement_date=day,
                       value_as_number=value, unit_source_value=unit)


def _crab(person):
    return _get_mm_specific_data(person).get('meets_crab')


def test_a_normal_si_creatinine_is_not_renal_evidence():
    person = PersonFactory()
    _creatinine(person, 80, 'umol/L')
    assert _crab(person) is None


def test_an_old_abnormal_value_does_not_outlive_a_newer_normal_one():
    person = PersonFactory()
    _creatinine(person, 3.0, 'mg/dL', day=OLD)
    _creatinine(person, 1.0, 'mg/dL', day=NEW)
    assert _crab(person) is None


@pytest.mark.parametrize('value, unit', [(2.5, 'mg/dL'), (221, 'umol/L')])
def test_a_current_abnormal_creatinine_meets_crab(value, unit):
    person = PersonFactory()
    _creatinine(person, value, unit)
    assert _crab(person) is True


def test_clearance_still_takes_precedence():
    person = PersonFactory()
    _creatinine(person, 3.0, 'mg/dL')
    MeasurementFactory(person=person, measurement_source_value='2164-2', measurement_date=NEW,
                       value_as_number=80)
    assert _crab(person) is None


def test_a_newer_coded_ehr_result_is_seen():
    """EHR rows carry the LOINC on the concept and a display name as source value."""
    from tests.factories import ConceptFactory
    person = PersonFactory()
    _creatinine(person, 3.0, 'mg/dL', day=OLD)
    coded = ConceptFactory(concept_id=3016723, concept_code='2160-0', concept_name='Creatinine')
    MeasurementFactory(person=person, measurement_concept=coded, measurement_source_value='Creatinine',
                       measurement_date=NEW, value_as_number=1.0, unit_source_value='mg/dL')
    assert _crab(person) is None


def test_a_unitless_creatinine_with_unit_concept_zero_is_read_in_mg_dl():
    """Concept 0 is no unit concept: the mg/dL default applies, as with no
    concept at all, instead of an unreadable "No matching concept" unit."""
    from omop_core.models import Concept
    from tests.factories import ConceptFactory
    zero = Concept.objects.filter(pk=0).first() or ConceptFactory(
        concept_id=0, concept_code='No matching concept', concept_name='No matching concept')
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='2160-0', measurement_date=NEW,
                       value_as_number=2.5, unit_source_value='', unit_concept=zero)
    assert _crab(person) is True


def test_a_creatinine_beyond_its_column_is_not_renal_evidence():
    person = PersonFactory()
    _creatinine(person, 5000, 'mg/dL')
    assert _crab(person) is None
