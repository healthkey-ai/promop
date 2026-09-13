import copy
import json

import pytest

from omop_core.models import FieldChoice, Measurement, PatientRecord, RecordRevision
from omop_core.services.projection_refresh_plan import (
    AUDIT_PATH, PlanConflict, apply_plan, create_plan,
)
from patient_portal.models import AuditEvent
from tests.factories import ConceptFactory, MeasurementFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db


def stale_record():
    record = PatientRecordFactory()
    question = ConceptFactory(concept_code='718-7', concept_name='Hemoglobin [Mass/volume] in Blood')
    fact = MeasurementFactory(person=record.person, measurement_concept=question,
                              value_as_number=12.4, unit_source_value='g/dL')
    PatientRecord.objects.filter(pk=record.pk).update(hemoglobin_g_dl=9, user_edited_fields=[])
    return record, fact


def test_preview_apply_resume_and_audited_rollback_preserve_facts():
    record, fact = stale_record()
    before = PatientRecord.objects.values().get(pk=record.pk)
    fact_before = Measurement.objects.values().get(pk=fact.pk)
    plan = create_plan([record.person_id])
    assert plan['entries'][0]['changes']['hemoglobin_g_dl'] == {'before': '9.0', 'after': '12.4'}
    assert PatientRecord.objects.values().get(pk=record.pk) == before
    assert not AuditEvent.objects.filter(path=AUDIT_PATH).exists()
    assert apply_plan(plan) == {'applied': 1}
    assert apply_plan(plan) == {'already_applied': 1}
    revision = RecordRevision.objects.get(patient_record=record, field='hemoglobin_g_dl')
    assert json.loads(revision.old_value) == '9.0'
    assert apply_plan(plan, rollback=True) == {'rolled_back': 1}
    assert apply_plan(plan, rollback=True) == {'already_rolled_back': 1}
    assert PatientRecord.objects.values().get(pk=record.pk) == before
    assert Measurement.objects.values().get(pk=fact.pk) == fact_before
    for event in AuditEvent.objects.filter(path=AUDIT_PATH):
        assert event.signature == event.compute_signature()
    with pytest.raises(PlanConflict, match='rolled back'):
        apply_plan(plan)


def test_pending_clear_survives_preview_and_application():
    record, _ = stale_record()
    PatientRecord.objects.filter(pk=record.pk).update(hemoglobin_g_dl=None, user_edited_fields=['hemoglobin_g_dl'])
    plan = create_plan([record.person_id])
    assert 'hemoglobin_g_dl' not in plan['entries'][0]['changes']
    apply_plan(plan)
    record.refresh_from_db()
    assert record.hemoglobin_g_dl is None
    assert 'hemoglobin_g_dl' in record.user_edited_fields


@pytest.mark.parametrize('drift', ['record', 'source', 'curation'])
def test_drift_refuses_application_without_partial_record_changes(drift):
    record, fact = stale_record()
    plan = create_plan([record.person_id])
    if drift == 'record':
        PatientRecord.objects.filter(pk=record.pk).update(hemoglobin_g_dl=8)
    elif drift == 'source':
        Measurement.objects.filter(pk=fact.pk).update(value_as_number=13)
    else:
        FieldChoice.objects.create(field_name='her2_status', display='New choice')
    before = PatientRecord.objects.values().get(pk=record.pk)
    with pytest.raises(PlanConflict, match='changed'):
        apply_plan(plan)
    assert PatientRecord.objects.values().get(pk=record.pk) == before
    assert not AuditEvent.objects.filter(path=AUDIT_PATH).exists()


def test_rollback_cannot_overwrite_later_user_edit():
    record, _ = stale_record()
    plan = create_plan([record.person_id])
    apply_plan(plan)
    PatientRecord.objects.filter(pk=record.pk).update(stage='Later edit')
    with pytest.raises(PlanConflict, match='changed after'):
        apply_plan(plan, rollback=True)
    record.refresh_from_db()
    assert record.stage == 'Later edit'


def test_missing_recovery_audit_is_not_reported_as_never_applied():
    record, _ = stale_record()
    plan = create_plan([record.person_id])
    apply_plan(plan)
    AuditEvent.objects.filter(path=AUDIT_PATH).delete()
    with pytest.raises(PlanConflict, match='recovery audit are unavailable'):
        apply_plan(plan, rollback=True)


def test_plan_tampering_and_unbounded_scope_rejected():
    record, _ = stale_record()
    plan = copy.deepcopy(create_plan([record.person_id]))
    plan['entries'][0]['changes'] = {}
    with pytest.raises(PlanConflict, match='signature'):
        apply_plan(plan)
    for people in ([], list(range(101))):
        with pytest.raises(PlanConflict, match='Select between'):
            create_plan(people)


def test_interrupted_batch_resumes_without_reapplying_completed_record():
    first, _ = stale_record()
    second, fact = stale_record()
    plan = create_plan([first.person_id, second.person_id])
    old_value = fact.value_as_number
    Measurement.objects.filter(pk=fact.pk).update(value_as_number=18)
    with pytest.raises(PlanConflict, match='Derivation changed'):
        apply_plan(plan)
    assert AuditEvent.objects.filter(path=AUDIT_PATH, detail__operation='apply').count() == 1
    Measurement.objects.filter(pk=fact.pk).update(value_as_number=old_value)
    assert apply_plan(plan) == {'already_applied': 1, 'applied': 1}


def test_command_private_manifest_and_aggregate_output(tmp_path, capsys):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    record, _ = stale_record()
    path = tmp_path / 'refresh-plan.json'
    call_command('backfill_patient_records', '--plan', str(path), '--person-id', str(record.person_id))
    assert path.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert 'hemoglobin_g_dl' in output
    assert '12.4' not in output
    assert 'person_id' not in output
    original = path.read_bytes()
    with pytest.raises(CommandError, match='never overwritten'):
        call_command('backfill_patient_records', '--plan', str(path), '--person-id', str(record.person_id))
    assert path.read_bytes() == original
    call_command('backfill_patient_records', '--apply-plan', str(path))
    call_command('backfill_patient_records', '--rollback-plan', str(path))


def test_command_rejects_mixed_or_unbounded_scope(tmp_path):
    from django.core.management import call_command
    from django.core.management.base import CommandError
    path = str(tmp_path / 'plan.json')
    for args in (['--plan', path], ['--plan', path, '--all'], ['--person-id', '1'],
                 ['--apply-plan', path, '--person-id', '1']):
        with pytest.raises(CommandError):
            call_command('backfill_patient_records', *args)
