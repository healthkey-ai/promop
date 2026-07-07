"""
Tests for enrich_breast_cancer_omop_data management command.

Covers:
  - Backfilling null ECOG/Karnofsky/stage Measurement values
  - Inserting missing tobacco/staging/best_response Observation rows
  - Idempotency (re-running doesn't duplicate rows or re-randomize values)
  - --dry-run makes no persisted changes
  - patient_record reflects the enriched OMOP data afterwards
"""
import pytest
from django.core.management import call_command

from omop_core.models import Measurement, Observation, PatientRecord
from tests.factories import (
    ConceptFactory, PersonFactory, PatientRecordFactory,
    MeasurementFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _loinc_concept(code, name):
    vocab = VocabularyFactory(vocabulary_id='LOINC')
    return ConceptFactory(concept_code=code, concept_name=name, vocabulary=vocab)


class TestPerformanceAndStageBackfill:

    def test_backfills_null_ecog_value(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IIA')
        ecog_concept = _loinc_concept('89247-1', 'ECOG Performance Status score')
        m = MeasurementFactory(person=person, measurement_concept=ecog_concept, value_as_number=None)

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        m.refresh_from_db()
        assert m.value_as_number in (0, 1, 2)

    def test_backfills_stage_measurement_from_existing_patient_record_stage(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IIIB')
        stage_concept = _loinc_concept('21908-9', 'Stage group.clinical Cancer')
        m = MeasurementFactory(person=person, measurement_concept=stage_concept, value_as_string=None)

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        m.refresh_from_db()
        assert m.value_as_string == 'Stage IIIB'

    def test_does_not_touch_already_populated_values(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='I')
        ecog_concept = _loinc_concept('89247-1', 'ECOG Performance Status score')
        m = MeasurementFactory(person=person, measurement_concept=ecog_concept, value_as_number=3)

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        m.refresh_from_db()
        assert m.value_as_number == 3


class TestMissingObservations:

    def test_creates_tobacco_status_observation(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        codes = set(
            Observation.objects.filter(person=person)
            .values_list('observation_concept__concept_code', flat=True)
        )
        assert codes & {'266919005', '8517006', '77176002'}

    def test_creates_staging_observations_consistent_with_existing_stage(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IV')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        t_obs = Observation.objects.get(person=person, observation_concept__concept_code='21905-5')
        m_obs = Observation.objects.get(person=person, observation_concept__concept_code='21901-4')
        assert t_obs.value_as_string == 'T4'
        assert m_obs.value_as_string == 'M1'

    def test_idempotent_second_run_does_not_duplicate(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))
        first_count = Observation.objects.filter(person=person).count()

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))
        second_count = Observation.objects.filter(person=person).count()

        assert first_count == second_count


class TestWearableMeasurements:

    def test_creates_at_least_min_valid_days_of_steps(self):
        from omop_core.services.mappings import WEARABLE_MIN_VALID_DAYS

        person = PersonFactory()
        PatientRecordFactory(person=person, stage='I')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        steps_rows = Measurement.objects.filter(
            person=person, measurement_concept__concept_code='55423-8',
        ).count()
        assert steps_rows >= WEARABLE_MIN_VALID_DAYS


class TestDryRun:

    def test_dry_run_persists_nothing(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')
        ecog_concept = _loinc_concept('89247-1', 'ECOG Performance Status score')
        MeasurementFactory(person=person, measurement_concept=ecog_concept, value_as_number=None)

        obs_before = Observation.objects.filter(person=person).count()
        meas_before = Measurement.objects.filter(person=person).count()

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), dry_run=True)

        assert Observation.objects.filter(person=person).count() == obs_before
        assert Measurement.objects.filter(person=person).count() == meas_before


class TestRefreshesPatientRecord:

    def test_patient_record_reflects_enriched_data(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

        record = PatientRecord.objects.get(person=person)
        assert record.no_tobacco_use_status is not None or record.tobacco_use_details is not None
