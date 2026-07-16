"""
Tests for benchmark_patient_record management command.

Covers:
  - Runs end-to-end against a small fixture cohort, both timed paths
    produce non-empty stats.
  - Never calls PatientRecord.save() — guards the read-only claim.
  - --org-slugs cohort selection works.
  - Empty cohort raises CommandError.
"""
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import Organization, PatientRecord
from tests.factories import PersonFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


class TestBenchmarkRun:

    def test_runs_and_reports_stats_for_person_ids(self, capsys):
        p1 = PersonFactory()
        PatientRecordFactory(person=p1, disease='Breast Cancer', stage='II')
        p2 = PersonFactory()
        PatientRecordFactory(person=p2, disease='Breast Cancer', stage='III')

        call_command(
            'benchmark_patient_record',
            person_ids=f'{p1.person_id},{p2.person_id}',
        )

        out = capsys.readouterr().out
        assert 'patient_record read:' in out
        assert 'OMOP-direct derive:' in out
        assert 'faster than live OMOP derivation' in out

    def test_empty_cohort_raises_command_error(self):
        with pytest.raises(CommandError):
            call_command('benchmark_patient_record', person_ids='999999999')

    def test_org_slug_cohort_selection(self, capsys):
        org = Organization.objects.create(name='Test Org', slug='test-org')
        person = PersonFactory()
        PatientRecordFactory(
            person=person, disease='Breast Cancer', stage='I', organization=org,
        )

        call_command('benchmark_patient_record', org_slugs='test-org')

        out = capsys.readouterr().out
        assert 'Benchmarking 1 patient(s)' in out

    def test_output_file_written(self, tmp_path):
        person = PersonFactory()
        PatientRecordFactory(person=person, disease='Breast Cancer', stage='I')
        output_path = tmp_path / 'results.json'

        call_command(
            'benchmark_patient_record',
            person_ids=str(person.person_id),
            output=str(output_path),
        )

        assert output_path.exists()


class TestReadOnlyGuarantee:

    def test_never_calls_patient_record_save(self, monkeypatch):
        person = PersonFactory()
        PatientRecordFactory(person=person, disease='Breast Cancer', stage='II')

        def _forbidden_save(self, *args, **kwargs):
            raise AssertionError('benchmark_patient_record must never call PatientRecord.save()')

        monkeypatch.setattr(PatientRecord, 'save', _forbidden_save)

        call_command('benchmark_patient_record', person_ids=str(person.person_id))
