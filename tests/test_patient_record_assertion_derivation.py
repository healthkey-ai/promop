"""#505 regression tests for dated OMOP assertion projection."""

from datetime import date

import pytest

from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    refresh_patient_record,
)
from tests.factories import (
    ConceptFactory,
    MeasurementFactory,
    ObservationFactory,
    PatientRecordFactory,
    PersonFactory,
)


pytestmark = pytest.mark.django_db


def test_latest_typed_assertions_project_from_observation_and_measurement():
    """Both OMOP fact tables are valid; newer dated assertion wins."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    contraception = ConceptFactory(concept_code='8659-8', concept_name='Contraceptive use')
    mental_health = ConceptFactory(concept_code='75618-3', concept_name='Mental health disorders')
    pregnancy = ConceptFactory(concept_code='2106-3', concept_name='Pregnancy test')

    ObservationFactory(
        person=person, observation_concept=contraception,
        observation_source_value='8659-8', observation_date=date(2026, 1, 1),
        value_as_string='yes',
    )
    MeasurementFactory(
        person=person, measurement_concept=contraception,
        measurement_source_value='8659-8', measurement_date=date(2026, 2, 1),
        value_as_number=0,
    )
    ObservationFactory(
        person=person, observation_concept=mental_health,
        observation_source_value='75618-3', observation_date=date(2026, 2, 2),
        value_as_string='no',
    )
    MeasurementFactory(
        person=person, measurement_concept=pregnancy,
        measurement_source_value='2106-3', measurement_date=date(2026, 2, 3),
        value_as_string='Negative',
    )

    record = refresh_patient_record(person)

    assert record.contraceptive_use is False
    assert record.no_mental_health_disorder_status is True
    assert record.pregnancy_test_result_value == 'Negative'
    assert record.pregnancy_test_date == date(2026, 2, 3)


def test_invalid_or_erroneous_assertions_do_not_create_projection_values():
    """Unknown text and entered-in-error rows cannot manufacture an answer."""
    person = PersonFactory()
    PatientRecordFactory(person=person, consent_capability=True)
    concept = ConceptFactory(concept_code='75985-6', concept_name='Ability to consent')
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='75985-6', measurement_date=date(2026, 1, 1),
        value_as_number=1,
    )
    ObservationFactory(
        person=person, observation_concept=concept,
        observation_source_value='75985-6', observation_date=date(2026, 2, 1),
        value_as_string='perhaps',
    )
    MeasurementFactory(
        person=person, measurement_concept=concept,
        measurement_source_value='75985-6', value_as_number=1, is_erroneous=True,
    )

    assert refresh_patient_record(person).consent_capability is None


def test_assertion_is_cleared_after_its_omop_source_is_deleted():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code='74204-0', concept_name='Non-prescription drug use')
    assertion = ObservationFactory(
        person=person, observation_concept=concept,
        observation_source_value='74204-0', value_as_string='no',
    )

    assert refresh_patient_record(person).no_substance_use_status is True
    assertion.delete()
    assert refresh_patient_record(person).no_substance_use_status is None


def test_implemented_assertion_fields_are_part_of_read_only_contract():
    assert {
        'pregnancy_test_date', 'pregnancy_test_result_value',
        'contraceptive_use', 'consent_capability', 'caregiver_availability_status',
        'no_mental_health_disorder_status', 'no_substance_use_status',
        'no_geographic_exposure_risk',
    } <= PATIENT_RECORD_OMOP_MAPPED_FIELDS
