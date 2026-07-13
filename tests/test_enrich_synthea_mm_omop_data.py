"""
Tests for enrich_synthea_mm_omop_data management command.
"""
import json
from datetime import date
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import ConditionOccurrence, Organization, ProcedureOccurrence
from tests.factories import PersonFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


def _write_mm_bundle(path: Path, given_name: str, family_name: str, birth_date: str, condition_code: str):
    bundle = {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [
            {
                'fullUrl': 'urn:uuid:patient-1',
                'resource': {
                    'resourceType': 'Patient',
                    'id': '1',
                    'name': [{'given': [given_name], 'family': family_name}],
                    'birthDate': birth_date,
                },
            },
            {
                'fullUrl': 'urn:uuid:condition-1',
                'resource': {
                    'resourceType': 'Condition',
                    'id': 'cond-1',
                    'subject': {'reference': 'Patient/1'},
                    'code': {
                        'coding': [{
                            'system': 'http://snomed.info/sct',
                            'code': condition_code,
                            'display': 'Multiple myeloma',
                        }],
                        'text': 'Multiple myeloma',
                    },
                    'onsetDateTime': '2020-01-10T00:00:00',
                },
            },
        ],
    }
    path.write_text(json.dumps(bundle))


class TestEnrichSyntheaMmOmopData:

    def test_backfills_condition_and_two_procedures(self, tmp_path):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        person = PersonFactory(
            given_name='Jane',
            family_name='Doe',
            year_of_birth=1968,
            month_of_birth=1,
            day_of_birth=2,
        )
        PatientRecordFactory(
            person=person,
            organization=org,
            disease='multiple myeloma',
            diagnosis_date=date(2020, 1, 10),
        )
        bundle_path = tmp_path / 'synthea_mm.json'
        _write_mm_bundle(bundle_path, 'Jane', 'Doe', '1968-01-02', 'SYNTH-MM-001')

        call_command(
            'enrich_synthea_mm_omop_data',
            bundle=str(bundle_path),
            person_ids=str(person.person_id),
            confirm=True,
        )

        assert ConditionOccurrence.objects.filter(person=person).count() == 1
        assert ProcedureOccurrence.objects.filter(person=person).count() == 2

    def test_idempotent_second_run(self, tmp_path):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        person = PersonFactory(
            given_name='Jane',
            family_name='Doe',
            year_of_birth=1968,
            month_of_birth=1,
            day_of_birth=2,
        )
        PatientRecordFactory(
            person=person,
            organization=org,
            disease='multiple myeloma',
            diagnosis_date=date(2020, 1, 10),
        )
        bundle_path = tmp_path / 'synthea_mm.json'
        _write_mm_bundle(bundle_path, 'Jane', 'Doe', '1968-01-02', 'SYNTH-MM-001')

        call_command(
            'enrich_synthea_mm_omop_data',
            bundle=str(bundle_path),
            person_ids=str(person.person_id),
            confirm=True,
        )
        first_condition_count = ConditionOccurrence.objects.filter(person=person).count()
        first_procedure_count = ProcedureOccurrence.objects.filter(person=person).count()

        call_command(
            'enrich_synthea_mm_omop_data',
            bundle=str(bundle_path),
            person_ids=str(person.person_id),
            confirm=True,
        )

        assert ConditionOccurrence.objects.filter(person=person).count() == first_condition_count
        assert ProcedureOccurrence.objects.filter(person=person).count() == first_procedure_count

    def test_dry_run_persists_nothing(self, tmp_path):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        person = PersonFactory(
            given_name='Jane',
            family_name='Doe',
            year_of_birth=1968,
            month_of_birth=1,
            day_of_birth=2,
        )
        PatientRecordFactory(
            person=person,
            organization=org,
            disease='multiple myeloma',
            diagnosis_date=date(2020, 1, 10),
        )
        bundle_path = tmp_path / 'synthea_mm.json'
        _write_mm_bundle(bundle_path, 'Jane', 'Doe', '1968-01-02', 'SYNTH-MM-001')

        call_command(
            'enrich_synthea_mm_omop_data',
            bundle=str(bundle_path),
            person_ids=str(person.person_id),
            dry_run=True,
        )

        assert ConditionOccurrence.objects.filter(person=person).count() == 0
        assert ProcedureOccurrence.objects.filter(person=person).count() == 0

    def test_requires_confirm_for_writes(self, tmp_path):
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        person = PersonFactory(
            given_name='Jane',
            family_name='Doe',
            year_of_birth=1968,
            month_of_birth=1,
            day_of_birth=2,
        )
        PatientRecordFactory(
            person=person,
            organization=org,
            disease='multiple myeloma',
            diagnosis_date=date(2020, 1, 10),
        )
        bundle_path = tmp_path / 'synthea_mm.json'
        _write_mm_bundle(bundle_path, 'Jane', 'Doe', '1968-01-02', 'SYNTH-MM-001')

        with pytest.raises(CommandError):
            call_command(
                'enrich_synthea_mm_omop_data',
                bundle=str(bundle_path),
                person_ids=str(person.person_id),
            )


def _write_mm_bundle_with_therapy(path: Path, given_name: str, family_name: str,
                                  birth_date: str, condition_code: str):
    bundle = {
        'resourceType': 'Bundle',
        'type': 'collection',
        'entry': [
            {
                'fullUrl': 'urn:uuid:patient-1',
                'resource': {
                    'resourceType': 'Patient',
                    'id': '1',
                    'name': [{'given': [given_name], 'family': family_name}],
                    'birthDate': birth_date,
                },
            },
            {
                'fullUrl': 'urn:uuid:condition-1',
                'resource': {
                    'resourceType': 'Condition',
                    'id': 'cond-1',
                    'subject': {'reference': 'Patient/1'},
                    'code': {
                        'coding': [{
                            'system': 'http://snomed.info/sct',
                            'code': condition_code,
                            'display': 'Multiple myeloma',
                        }],
                        'text': 'Multiple myeloma',
                    },
                    'onsetDateTime': '2020-01-10T00:00:00',
                },
            },
            {
                'fullUrl': 'urn:uuid:med-1',
                'resource': {
                    'resourceType': 'MedicationStatement',
                    'id': 'med-1',
                    'status': 'completed',
                    'subject': {'reference': 'Patient/1'},
                    'extension': [
                        {'url': 'https://healthkey.ai/fhir/StructureDefinition/therapy-line',
                         'valueInteger': 1},
                        {'url': 'https://healthkey.ai/fhir/StructureDefinition/therapy-outcome',
                         'valueString': 'Partial Response'},
                    ],
                    'medicationCodeableConcept': {'text': 'VRd'},
                    'effectivePeriod': {'start': '2020-02-01', 'end': '2020-08-01'},
                },
            },
        ],
    }
    path.write_text(json.dumps(bundle))


class TestEnrichSyntheaMmLotOutcomes:

    def _setup_patient(self, tmp_path):
        from tests.factories import ConceptFactory

        # Standard OMOP "No matching concept" row — always present in real DBs.
        ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='No matching concept')
        org = Organization.objects.create(name='SYNTHEA-MM', slug='synthea-mm')
        person = PersonFactory(
            given_name='Jane',
            family_name='Doe',
            year_of_birth=1968,
            month_of_birth=1,
            day_of_birth=2,
        )
        PatientRecordFactory(
            person=person,
            organization=org,
            disease='multiple myeloma',
            diagnosis_date=date(2020, 1, 10),
        )
        bundle_path = tmp_path / 'synthea_mm_therapy.json'
        _write_mm_bundle_with_therapy(bundle_path, 'Jane', 'Doe', '1968-01-02', 'SYNTH-MM-001')
        return person, bundle_path

    def test_writes_lot_outcome_observation(self, tmp_path):
        from omop_core.models import Observation

        person, bundle_path = self._setup_patient(tmp_path)

        call_command(
            'enrich_synthea_mm_omop_data',
            bundle=str(bundle_path),
            person_ids=str(person.person_id),
            confirm=True,
        )

        outcome_obs = Observation.objects.filter(
            person=person, observation_source_value='LOT-1-outcome',
        )
        assert outcome_obs.count() == 1
        assert outcome_obs.first().value_as_string == 'Partial Response'

    def test_lot_outcome_observation_idempotent(self, tmp_path):
        from omop_core.models import Observation

        person, bundle_path = self._setup_patient(tmp_path)

        for _ in range(2):
            call_command(
                'enrich_synthea_mm_omop_data',
                bundle=str(bundle_path),
                person_ids=str(person.person_id),
                confirm=True,
            )

        assert Observation.objects.filter(
            person=person, observation_source_value='LOT-1-outcome',
        ).count() == 1

    def test_refreshed_patient_record_gets_first_line_outcome(self, tmp_path):
        from omop_core.models import PatientRecord

        person, bundle_path = self._setup_patient(tmp_path)

        call_command(
            'enrich_synthea_mm_omop_data',
            bundle=str(bundle_path),
            person_ids=str(person.person_id),
            confirm=True,
        )

        record = PatientRecord.objects.get(person=person)
        assert record.first_line_outcome == 'Partial Response'
