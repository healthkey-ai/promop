"""Behavior/lifestyle fields derived from OMOP facts."""

from datetime import date

import pytest

from omop_core.models import Measurement, Observation
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import (
    ConceptFactory, MeasurementFactory, ObservationFactory, PatientRecordFactory,
    PersonFactory,
)

pytestmark = pytest.mark.django_db


def test_sleep_hours_per_night_derives_from_sleep_duration_measurement():
    person = PersonFactory()
    PatientRecordFactory(person=person, sleep_hours_per_night=4.0)
    sleep_duration = ConceptFactory(
        concept_code='93832-4',
        concept_name='Sleep duration',
    )

    MeasurementFactory(
        person=person,
        measurement_concept=sleep_duration,
        measurement_source_value='93832-4',
        measurement_date=date(2026, 1, 1),
        value_as_number=6.5,
    )
    MeasurementFactory(
        person=person,
        measurement_concept=sleep_duration,
        measurement_source_value='93832-4',
        measurement_date=date(2026, 1, 2),
        value_as_number=7.25,
    )

    record = refresh_patient_record(person)

    assert float(record.sleep_hours_per_night) == 7.25

    Measurement.objects.filter(person=person).delete()

    record = refresh_patient_record(person)

    assert record.sleep_hours_per_night is None


def test_sleep_hours_per_night_accepts_source_value_fallback():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    fallback_concept = ConceptFactory(
        concept_code='0',
        concept_name='No matching concept',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=fallback_concept,
        measurement_source_value='93832-4',
        value_as_number=8,
    )

    record = refresh_patient_record(person)

    assert float(record.sleep_hours_per_night) == 8.0


def test_withdrawn_tobacco_answer_is_cleared():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    status = ConceptFactory(
        concept_code='72166-2', vocabulary_id='LOINC',
        concept_name='Tobacco smoking status',
    )
    never = ConceptFactory(
        concept_code='LA18978-9', vocabulary_id='LOINC',
        concept_name='Never smoker',
    )
    ObservationFactory(
        person=person, observation_concept=status,
        observation_source_value='72166-2', value_as_concept=never,
        observation_date=date(2026, 1, 1),
    )

    record = refresh_patient_record(person)
    assert record.no_tobacco_use_status is True
    assert record.tobacco_use_details == 'Never smoker'

    Observation.objects.filter(person=person).delete()
    record = refresh_patient_record(person)
    assert record.no_tobacco_use_status is None
    assert record.tobacco_use_details is None
