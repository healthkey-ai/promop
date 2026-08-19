"""Guards around management commands that historically wrote PatientRecord."""

import pytest
from django.core.management import call_command
from django.test import override_settings

from omop_core.management.commands.backfill_therapy_concept_ids import Command
from omop_core.models import DrugExposure
from omop_core.services.patient_record_service import refresh_patient_record
from tests.factories import ConceptFactory, DrugExposureFactory, PatientRecordFactory, PersonFactory


pytestmark = pytest.mark.django_db


def test_therapy_backfill_rederives_from_omop_instead_of_projection_text(capsys):
    person = PersonFactory()
    record = PatientRecordFactory(person=person, first_line_therapy='stale projection')
    drug = ConceptFactory(concept_name='OMOP regimen component')
    DrugExposureFactory(person=person, drug_concept=drug)

    expected = refresh_patient_record(person).first_line_therapy
    record.first_line_therapy = 'stale projection'
    record.save(update_fields=['first_line_therapy'])

    call_command('backfill_therapy_concept_ids')

    record.refresh_from_db()
    assert record.first_line_therapy == expected
    assert 're-derived=1' in capsys.readouterr().out


def test_therapy_backfill_dry_run_does_not_modify_projection(capsys):
    record = PatientRecordFactory(first_line_therapy='projection value')

    call_command('backfill_therapy_concept_ids', '--dry-run')

    record.refresh_from_db()
    assert record.first_line_therapy == 'projection value'
    assert '[DRY RUN]' in capsys.readouterr().out


@override_settings(DEBUG=True)
def test_seed_test_patients_requires_explicit_fixture_acknowledgement(capsys):
    call_command('seed_test_patients')

    assert 'Refusing to create fixtures' in capsys.readouterr().err


@override_settings(DEBUG=False)
def test_seed_test_patients_is_refused_outside_debug_mode(capsys):
    call_command('seed_test_patients', '--allow-test-fixtures')

    assert 'DEBUG-only' in capsys.readouterr().err


def test_reverse_projection_backfill_has_no_direct_patientrecord_update():
    source = Command.__module__
    assert source == 'omop_core.management.commands.backfill_therapy_concept_ids'
    command_path = __import__(source, fromlist=['__file__']).__file__
    command_source = open(command_path).read()
    assert 'PatientRecord.objects.filter(pk=' not in command_source
    assert 'refresh_patient_record(record.person)' in command_source
