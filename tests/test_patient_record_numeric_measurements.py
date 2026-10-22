"""#504 regression tests for remaining numeric Measurement projections."""

from datetime import date

import pytest

from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _clear_overflowing_decimal_fields,
    refresh_patient_record,
)
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory, PersonFactory


pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ('code', 'field', 'value'),
    [
        ('80404-7', 'heartrate_variability', 42),
        ('8806-2', 'ejection_fraction', 61),
        ('8632-1', 'qtcf_value', 432.5),
        ('1920-8', 'liver_enzyme_levels_ast', 25),
        ('1742-6', 'liver_enzyme_levels_alt', 31),
        ('6768-6', 'liver_enzyme_levels_alp', 88),
    ],
)
def test_numeric_measurements_project_and_clear(code, field, value):
    """Each #504 field is a projection of one dated OMOP Measurement only."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code=code, concept_name=f'LOINC {code}')
    measurement = MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value=code, measurement_date=date(2026, 8, 18),
        value_as_number=value,
    )

    assert float(getattr(refresh_patient_record(person), field)) == value

    measurement.delete()
    assert getattr(refresh_patient_record(person), field) is None


def test_numeric_measurement_source_code_fallback_is_supported():
    """An unresolved vocabulary concept still carries its reviewed LOINC source."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    generic = ConceptFactory(concept_code='generic-lab', concept_name='Generic laboratory result')
    MeasurementFactory(
        person=person, measurement_concept=generic,
        measurement_source_value='8632-1', measurement_date=date(2026, 8, 18),
        value_as_number=430,
    )

    assert refresh_patient_record(person).qtcf_value == pytest.approx(430.0)


def test_overflowing_measurement_does_not_abort_other_derived_values(caplog):
    """A corrupt absolute platelet count must not discard the whole projection."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    platelets = ConceptFactory(concept_code='777-3', concept_name='Platelets [#/volume] in Blood')
    qtcf = ConceptFactory(concept_code='8632-1', concept_name='QTcF')
    MeasurementFactory(
        person=person, measurement_concept=platelets,
        measurement_source_value='777-3', measurement_date=date(2026, 8, 18),
        value_as_number=251_000_000,
        unit_source_value='10*3/uL',
    )
    MeasurementFactory(
        person=person, measurement_concept=qtcf,
        measurement_source_value='8632-1', measurement_date=date(2026, 8, 18),
        value_as_number=432,
    )

    record = refresh_patient_record(person)

    assert record.platelet_count_thousand_per_ul is None
    assert record.qtcf_value == 432
    assert 'platelet_count_thousand_per_ul' in caplog.text
    assert '251000000' in caplog.text


def test_decimal_rounding_that_carries_into_overflow_is_skipped():
    record = PatientRecordFactory.build(platelet_count_thousand_per_ul=99_999.95)

    _clear_overflowing_decimal_fields(record)

    assert record.platelet_count_thousand_per_ul is None


def test_retired_liver_enzyme_composite_cannot_retain_projection_value():
    """One scalar cannot safely stand in for three distinct liver analytes."""
    record = PatientRecordFactory(liver_enzyme_levels=99)

    assert refresh_patient_record(record.person).liver_enzyme_levels is None


def test_numeric_measurement_fields_are_read_only_patientrecord_projections():
    assert {
        'heartrate_variability', 'ejection_fraction', 'qtcf_value',
        'liver_enzyme_levels_ast', 'liver_enzyme_levels_alt',
        'liver_enzyme_levels_alp', 'liver_enzyme_levels',
    } <= PATIENT_RECORD_OMOP_MAPPED_FIELDS
