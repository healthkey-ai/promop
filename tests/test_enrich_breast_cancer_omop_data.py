"""
Tests for enrich_breast_cancer_omop_data management command.

Covers:
  - Backfilling null ECOG/Karnofsky/stage Measurement values
  - Inserting missing tobacco/staging/response-status Observation rows
  - Idempotency (re-running doesn't duplicate rows or re-randomize values)
  - --dry-run makes no persisted changes
  - patient_record reflects the enriched OMOP data afterwards
"""
from io import StringIO

import pytest
from django.core.management import call_command, CommandError

from omop_core.models import DrugExposure, Measurement, Observation, PatientRecord
from omop_core.services.mappings import (
    WEARABLE_CONCEPT_CODE, WEARABLE_CONCEPT_VOCAB,
)
from tests.factories import (
    ConceptFactory, DomainFactory, PersonFactory, PatientRecordFactory,
    MeasurementFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db


def _loinc_concept(code, name, vocabulary_id='LOINC', domain_id='Measurement'):
    vocab = VocabularyFactory(vocabulary_id=vocabulary_id)
    domain = DomainFactory(domain_id=domain_id)
    return ConceptFactory(
        concept_code=code, concept_name=name, vocabulary=vocab, domain=domain)


@pytest.fixture(autouse=True)
def _seed_loinc_concepts_the_command_assumes_exist():
    """enrich_breast_cancer_omop_data assumes the staging and wearable concepts
    already exist (true on the real staging DB, where load_athena_vocabularies
    and seed_omop_concepts put them there). A fresh test DB has none, so seed
    them here.

    The wearable set is derived from WEARABLE_CONCEPT_CODE rather than
    hard-coded: the command requires EVERY entry to resolve and aborts with
    CommandError on the first miss, so a literal list silently rots the moment a
    metric is added or a code corrected — which is exactly what happened when
    active_minutes moved off the IPAQ code in #413. Deriving it also gets the
    vocabulary and domain right for the HK-Wearable and Observation-domain
    metrics, which a LOINC/Measurement-only helper cannot express."""
    for code, name in {
        '21905-5': 'Primary tumor.clinical [Class] Cancer',
        '21901-4': 'Distant metastases.pathology [Class] Cancer',
    }.items():
        _loinc_concept(code, name)

    observation_domain = {
        'steps', 'active_minutes', 'sleep_duration', 'flights_climbed',
    }
    for metric_key, code in WEARABLE_CONCEPT_CODE.items():
        _loinc_concept(
            code,
            f'Wearable {metric_key}',
            vocabulary_id=WEARABLE_CONCEPT_VOCAB[metric_key],
            domain_id='Observation' if metric_key in observation_domain else 'Measurement',
        )


class TestPerformanceAndStageBackfill:

    def test_backfills_null_ecog_value(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IIA')
        ecog_concept = _loinc_concept('89247-1', 'ECOG Performance Status score')
        m = MeasurementFactory(person=person, measurement_concept=ecog_concept, value_as_number=None)

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        m.refresh_from_db()
        assert m.value_as_number in (0, 1, 2)

    def test_backfills_stage_measurement_from_existing_patient_record_stage(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IIIB')
        stage_concept = _loinc_concept('21908-9', 'Stage group.clinical Cancer')
        m = MeasurementFactory(person=person, measurement_concept=stage_concept, value_as_string=None)

        # Creating the Measurement above fires the OMOP post_save signal,
        # which calls refresh_patient_record() outside the command's own
        # suppress_patient_record_refresh() context. Since `stage` is itself
        # derived from this same (still-null) measurement, that refresh
        # clears it back to None — re-set it so the precondition this test
        # is actually checking (the command reads an existing stage value to
        # backfill the measurement) holds regardless of that side effect.
        PatientRecord.objects.filter(person=person).update(stage='IIIB')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        m.refresh_from_db()
        assert m.value_as_string == 'Stage IIIB'

    def test_does_not_touch_already_populated_values(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='I')
        ecog_concept = _loinc_concept('89247-1', 'ECOG Performance Status score')
        m = MeasurementFactory(person=person, measurement_concept=ecog_concept, value_as_number=3)

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        m.refresh_from_db()
        assert m.value_as_number == 3


class TestMissingObservations:

    def test_creates_tobacco_status_observation(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        codes = set(
            Observation.objects.filter(person=person)
            .values_list('observation_concept__concept_code', flat=True)
        )
        assert codes & {'266919005', '8517006', '77176002'}

    def test_creates_staging_observations_consistent_with_existing_stage(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IV')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        t_obs = Observation.objects.get(person=person, observation_concept__concept_code='21905-5')
        m_obs = Observation.objects.get(person=person, observation_concept__concept_code='21901-4')
        assert t_obs.value_as_string == 'T4'
        assert m_obs.value_as_string == 'M1'

    def test_idempotent_second_run_does_not_duplicate(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)
        first_count = Observation.objects.filter(person=person).count()

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)
        second_count = Observation.objects.filter(person=person).count()

        assert first_count == second_count


class TestWearableMeasurements:

    def test_creates_at_least_min_valid_days_of_steps(self):
        from omop_core.services.mappings import WEARABLE_MIN_VALID_DAYS

        person = PersonFactory()
        PatientRecordFactory(person=person, stage='I')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        # steps is an Observation-domain concept, so the command routes it to
        # `observation`; resting_hr is Measurement-domain and stays put. Rows are
        # routed by concept.domain_id rather than a hard-coded metric list.
        steps_rows = Observation.objects.filter(
            person=person, observation_concept__concept_code='55423-8',
        ).count()
        assert steps_rows >= WEARABLE_MIN_VALID_DAYS

        assert Measurement.objects.filter(
            person=person, measurement_concept__concept_code='55423-8',
        ).count() == 0, 'steps must not be written to measurement'

        rhr_rows = Measurement.objects.filter(
            person=person, measurement_concept__concept_code='40443-4',
        ).count()
        assert rhr_rows >= WEARABLE_MIN_VALID_DAYS


class TestDryRun:

    def test_requires_confirm_for_synthetic_writes(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')

        with pytest.raises(CommandError, match='writes synthetic OMOP rows'):
            call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id))

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

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        record = PatientRecord.objects.get(person=person)
        assert record.no_tobacco_use_status is not None or record.tobacco_use_details is not None

    def test_missing_therapy_backfill_creates_regimen_concept_and_patient_record_id(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='IV')

        call_command('enrich_breast_cancer_omop_data', person_ids=str(person.person_id), confirm=True)

        record = PatientRecord.objects.get(person=person)
        exposure = DrugExposure.objects.get(person=person)
        assert record.first_line_therapy_id is not None
        assert exposure.drug_concept_id == record.first_line_therapy_id
        assert record.first_line_therapy

    def test_refresh_is_deferred_until_after_all_patients_are_enriched(self, monkeypatch):
        person_a = PersonFactory()
        person_b = PersonFactory()
        PatientRecordFactory(person=person_a, stage='II')
        PatientRecordFactory(person=person_b, stage='IV')

        snapshots = []

        def fake_refresh(person):
            snapshots.append({
                'person_id': person.person_id,
                'person_a_observations': Observation.objects.filter(person=person_a).count(),
                'person_b_observations': Observation.objects.filter(person=person_b).count(),
            })

        monkeypatch.setattr(
            'omop_core.management.commands.enrich_breast_cancer_omop_data.refresh_patient_record',
            fake_refresh,
        )
        monkeypatch.setattr(
            'omop_core.services.patient_record_service.refresh_patient_record',
            fake_refresh,
        )

        call_command(
            'enrich_breast_cancer_omop_data',
            person_ids=f'{person_a.person_id},{person_b.person_id}',
            confirm=True,
        )

        assert [snapshot['person_id'] for snapshot in snapshots] == [
            person_a.person_id,
            person_b.person_id,
        ]
        assert snapshots[0]['person_a_observations'] > 0
        assert snapshots[0]['person_b_observations'] > 0

    def test_outputs_enrichment_and_refresh_progress(self):
        person = PersonFactory()
        PatientRecordFactory(person=person, stage='II')
        output = StringIO()

        call_command(
            'enrich_breast_cancer_omop_data',
            person_ids=str(person.person_id),
            stdout=output,
            confirm=True,
        )

        text = output.getvalue()
        assert 'Phase 1/2: Enrichment (1 patients)' in text
        assert f'[1/1  100.0%]  person_id={person.person_id}' in text
        assert 'Phase 2/2: PatientRecord refresh (1 patients)' in text
        assert 'refreshed in' in text

    def test_refresh_only_defaults_to_breast_cancer_records(self, monkeypatch):
        breast_person = PersonFactory()
        other_person = PersonFactory()
        org = PatientRecordFactory(person=breast_person, disease='Breast Cancer').organization
        PatientRecordFactory(person=other_person, organization=org, disease='Multiple Myeloma')
        refreshed = []

        monkeypatch.setattr(
            'omop_core.management.commands.enrich_breast_cancer_omop_data.refresh_patient_record',
            lambda person: refreshed.append(person.person_id),
        )

        call_command(
            'enrich_breast_cancer_omop_data',
            org_slugs=org.slug,
            refresh_only=True,
        )

        assert refreshed == [breast_person.person_id]

    def test_refresh_only_can_refresh_all_org_patients(self, monkeypatch):
        breast_person = PersonFactory()
        other_person = PersonFactory()
        org = PatientRecordFactory(person=breast_person, disease='Breast Cancer').organization
        PatientRecordFactory(person=other_person, organization=org, disease='Multiple Myeloma')
        refreshed = []

        monkeypatch.setattr(
            'omop_core.management.commands.enrich_breast_cancer_omop_data.refresh_patient_record',
            lambda person: refreshed.append(person.person_id),
        )

        call_command(
            'enrich_breast_cancer_omop_data',
            org_slugs=org.slug,
            refresh_only=True,
            refresh_all_org_patients=True,
        )

        assert refreshed == [breast_person.person_id, other_person.person_id]
