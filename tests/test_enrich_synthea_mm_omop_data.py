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
