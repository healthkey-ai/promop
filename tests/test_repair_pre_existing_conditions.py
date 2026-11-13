import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from omop_core.management.commands.repair_pre_existing_conditions import CONSTRAINT, REQUIRED
from omop_core.models import (
    FieldFormula, PatientRecord, PreexistingConditionsAttestation, RecordRevision,
)
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def ready():
    recorder = MigrationRecorder(connection)
    recorder.ensure_schema()
    for app, name in REQUIRED:
        recorder.record_applied(app, name)
    with connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE patient_record ADD CONSTRAINT {CONSTRAINT} '
                       "CHECK (no_pre_existing_conditions IS NOT TRUE OR "
                       "preexisting_conditions IS NULL OR "
                       "preexisting_conditions IN ('[]'::jsonb, 'null'::jsonb))")


def cohort():
    ambiguous = PatientRecordFactory(no_pre_existing_conditions=True, preexisting_conditions=[])
    explicit = PatientRecordFactory(no_pre_existing_conditions=True, preexisting_conditions=[])
    PreexistingConditionsAttestation.objects.create(
        person=explicit.person, source='patient-explicit', confirmed_at=timezone.now())
    negative = PatientRecordFactory(no_pre_existing_conditions=False, preexisting_conditions=[])
    unknown = PatientRecordFactory(no_pre_existing_conditions=None, preexisting_conditions=[])
    return ambiguous, explicit, negative, unknown


def audit(tmp_path, name='audit.json'):
    path = tmp_path / name
    call_command('audit_pre_existing_conditions', output=str(path), stdout=StringIO())
    return str(path), json.loads(path.read_text())['approval_sha256']


def repair(manifest, sha, **options):
    call_command('repair_pre_existing_conditions', manifest=manifest, expect_sha=sha,
                 stdout=StringIO(), **options)


def apply(tmp_path, manifest, sha, name='backup.json', **options):
    path = tmp_path / name
    repair(manifest, sha, apply=True, authoring_verified=True, backup=str(path), **options)
    return path, json.loads(path.read_text())


def rollback(tmp_path, path, receipt, **options):
    call_command('repair_pre_existing_conditions', rollback=str(path),
                 expect_sha=receipt['backup_sha256'], backup=str(tmp_path / 'undo.json'),
                 stdout=StringIO(), **options)


def test_dry_run_is_read_only(tmp_path):
    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    before = RecordRevision.objects.count()
    repair(manifest, sha)
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    assert RecordRevision.objects.count() == before


def test_apply_only_ambiguous_and_preserves_original_in_db_and_private_file(tmp_path, ready):
    ambiguous, explicit, negative, unknown = cohort()
    old = PreexistingConditionsAttestation.objects.create(
        person=ambiguous.person, source='patient', confirmed_at=timezone.now())
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(receipt['entries']) == 1
    assert receipt['entries'][0]['before']['flag'] is True
    assert receipt['entries'][0]['before']['attestation']['id'] == old.pk
    assert RecordRevision.objects.filter(patient_record=ambiguous,
                                         field='preexisting_conditions_repair').exists()
    for record, expected in ((ambiguous, None), (explicit, True), (negative, False), (unknown, None)):
        record.refresh_from_db()
        assert record.no_pre_existing_conditions is expected
    assert not PreexistingConditionsAttestation.objects.filter(person=ambiguous.person).exists()


def test_apply_aborts_when_an_attestation_lands_between_audit_and_lock(
        tmp_path, ready, monkeypatch):
    """The audit runs before the lock, so the repair must re-derive the evidence
    under the lock: a person confirming "none" before the write is not a default."""
    from omop_core.management.commands import repair_pre_existing_conditions as module

    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    original = module.Command._lock

    def lock_after_an_explicit_answer(self, live, supplied, rolling_back):
        PreexistingConditionsAttestation.objects.create(
            person=ambiguous.person, source='patient-explicit', confirmed_at=timezone.now())
        original(self, live, supplied, rolling_back)

    monkeypatch.setattr(module.Command, '_lock', lock_after_an_explicit_answer)
    with pytest.raises(CommandError, match='changed after the audit'):
        apply(tmp_path, manifest, sha)

    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    assert not RecordRevision.objects.filter(
        changed_by__startswith='repair:1988:').exists()


def test_apply_aborts_when_the_flag_changes_between_audit_and_lock(
        tmp_path, ready, monkeypatch):
    from omop_core.management.commands import repair_pre_existing_conditions as module

    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    original = module.Command._lock

    def lock_after_the_answer_changed(self, live, supplied, rolling_back):
        PatientRecord.objects.filter(pk=ambiguous.pk).update(
            no_pre_existing_conditions=False)
        original(self, live, supplied, rolling_back)

    monkeypatch.setattr(module.Command, '_lock', lock_after_the_answer_changed)
    with pytest.raises(CommandError, match='changed after the audit'):
        apply(tmp_path, manifest, sha)


@pytest.mark.parametrize('change', ['flag', 'categories', 'evidence', 'digest', 'target', 'classification', 'id'])
def test_changed_data_evidence_or_manifest_is_refused(tmp_path, ready, change):
    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    if change == 'flag':
        ambiguous.no_pre_existing_conditions = None
        ambiguous.save(update_fields=['no_pre_existing_conditions'])
    elif change == 'categories':
        ambiguous.preexisting_conditions = None
        ambiguous.save(update_fields=['preexisting_conditions'])
    elif change == 'evidence':
        PreexistingConditionsAttestation.objects.create(
            person=ambiguous.person, source='patient-explicit', confirmed_at=timezone.now())
    elif change == 'digest':
        sha = '0' * 64
    else:
        data = json.loads(open(manifest).read())
        if change == 'target':
            data['database']['name'] = 'different'
        elif change == 'classification':
            data['records'][0]['classification'] = 'authored_true'
        else:
            data['records'][0]['id'] += 999
        with open(manifest, 'w') as stream:
            json.dump(data, stream)
    with pytest.raises(CommandError, match='mismatch'):
        apply(tmp_path, manifest, sha)
    assert not (tmp_path / 'backup.json').exists()


def test_formula_still_active_blocks_apply(tmp_path, ready, stale_answer_formula_allowed):
    cohort()
    FieldFormula.objects.create(field_name='no_pre_existing_conditions', formula='1', is_active=True)
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='still active'):
        apply(tmp_path, manifest, sha)


def test_migrations_and_constraint_are_required(tmp_path):
    cohort()
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='migrations'):
        apply(tmp_path, manifest, sha)


def test_missing_constraint_is_refused(tmp_path, ready):
    with connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE patient_record DROP CONSTRAINT {CONSTRAINT}')
    cohort()
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='CHECK constraint'):
        apply(tmp_path, manifest, sha)


@pytest.mark.parametrize('weak', [True, False])
def test_fake_or_unvalidated_constraint_is_refused(tmp_path, ready, weak):
    with connection.cursor() as cursor:
        cursor.execute(f'ALTER TABLE patient_record DROP CONSTRAINT {CONSTRAINT}')
        if weak:
            cursor.execute(f'ALTER TABLE patient_record ADD CONSTRAINT {CONSTRAINT} CHECK (TRUE)')
        else:
            cursor.execute(f'ALTER TABLE patient_record ADD CONSTRAINT {CONSTRAINT} '
                           "CHECK (no_pre_existing_conditions IS NOT TRUE OR "
                           "preexisting_conditions IS NULL OR "
                           "preexisting_conditions IN ('[]'::jsonb, 'null'::jsonb)) NOT VALID")
    cohort()
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='unvalidated or unexpected'):
        apply(tmp_path, manifest, sha)


def test_apply_requires_operator_verification_and_fresh_backup(tmp_path, ready):
    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    with pytest.raises(CommandError, match='authoring-verified'):
        repair(manifest, sha, apply=True)
    (tmp_path / 'backup.json').write_text('do not overwrite')
    with pytest.raises(CommandError, match='exclusively create'):
        apply(tmp_path, manifest, sha)
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    assert (tmp_path / 'backup.json').read_text() == 'do not overwrite'
    assert not RecordRevision.objects.filter(field='preexisting_conditions_repair').exists()


def test_failed_backup_flush_rolls_back_db_and_leftover_file_is_not_a_commit(tmp_path, ready, monkeypatch):
    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    def fail_flush(_):
        raise OSError('simulated flush failure')
    monkeypatch.setattr('omop_core.management.guarded_repair.os.fsync', fail_flush)
    with pytest.raises(CommandError, match='persist the private backup'):
        apply(tmp_path, manifest, sha)
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    path = tmp_path / 'backup.json'
    receipt = json.loads(path.read_text())
    with pytest.raises(CommandError, match='Rollback conflict'):
        rollback(tmp_path, path, receipt)


def test_failed_directory_flush_aborts_apply(tmp_path, ready, monkeypatch):
    import os
    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    original = os.fsync
    calls = []
    def flush(fd):
        calls.append(fd)
        if len(calls) == 2:
            raise OSError('simulated directory flush failure')
        original(fd)
    monkeypatch.setattr('omop_core.management.guarded_repair.os.fsync', flush)
    with pytest.raises(CommandError, match='persist the private backup'):
        apply(tmp_path, manifest, sha)
    assert len(calls) == 2
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    assert not RecordRevision.objects.filter(field='preexisting_conditions_repair').exists()


def test_apply_is_not_repeated_against_old_manifest(tmp_path, ready):
    cohort()
    manifest, sha = audit(tmp_path)
    apply(tmp_path, manifest, sha)
    with pytest.raises(CommandError, match='mismatch'):
        apply(tmp_path, manifest, sha, name='second.json')


def test_guarded_rollback_restores_flag_and_legacy_evidence(tmp_path, ready):
    ambiguous, *_ = cohort()
    old = PreexistingConditionsAttestation.objects.create(
        person=ambiguous.person, source='patient', confirmed_at=timezone.now())
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    rollback(tmp_path, path, receipt)  # dry-run first
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is None
    rollback(tmp_path, path, receipt, apply=True, authoring_verified=True)
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    restored = PreexistingConditionsAttestation.objects.get(person=ambiguous.person)
    assert restored.source == old.source
    assert restored.confirmed_at == old.confirmed_at


@pytest.mark.parametrize('change', ['record', 'attestation', 'revision'])
def test_rollback_refuses_any_subsequent_edit(tmp_path, ready, change):
    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    if change == 'record':
        type(ambiguous).objects.filter(pk=ambiguous.pk).update(ecog_performance_status=2)
    elif change == 'attestation':
        PreexistingConditionsAttestation.objects.create(
            person=ambiguous.person, source='patient-explicit', confirmed_at=timezone.now())
    else:
        RecordRevision.objects.create(patient_record=ambiguous, field='email', new_value='changed')
    with pytest.raises(CommandError, match='Rollback conflict'):
        rollback(tmp_path, path, receipt, apply=True, authoring_verified=True)
    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is None
    assert not (tmp_path / 'undo.json').exists()


def test_rollback_conflict_aborts_the_entire_cohort(tmp_path, ready):
    first, *_ = cohort()
    second = PatientRecordFactory(no_pre_existing_conditions=True, preexisting_conditions=[])
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    type(second).objects.filter(pk=second.pk).update(ecog_performance_status=2)
    with pytest.raises(CommandError, match='Rollback conflict'):
        rollback(tmp_path, path, receipt, apply=True, authoring_verified=True)
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.no_pre_existing_conditions is None
    assert second.no_pre_existing_conditions is None


def test_tampered_backup_is_refused(tmp_path, ready):
    cohort()
    manifest, sha = audit(tmp_path)
    path, receipt = apply(tmp_path, manifest, sha)
    receipt['entries'][0]['before']['flag'] = False
    path.write_text(json.dumps(receipt))
    with pytest.raises(CommandError, match='digest mismatch'):
        rollback(tmp_path, path, receipt, apply=True, authoring_verified=True)


def test_target_guards_do_not_authorize_production_or_unknown_staging(tmp_path, ready, monkeypatch):
    cohort()
    manifest, sha = audit(tmp_path)
    monkeypatch.delenv('STAGING_DATABASE_URL', raising=False)
    with pytest.raises(CommandError, match='STAGING_DATABASE_URL'):
        apply(tmp_path, manifest, sha, environment='staging')
    cfg = connection.settings_dict
    monkeypatch.setenv('PROD_DATABASE_URL',
                       f"postgresql://unused@{cfg['HOST']}:{cfg['PORT']}/{cfg['NAME']}")
    with pytest.raises(CommandError, match='Production writes'):
        apply(tmp_path, manifest, sha)


@pytest.mark.parametrize('inherited', ['empty_host', 'service'])
def test_inherited_connection_targets_are_not_accepted_as_local(tmp_path, ready, monkeypatch, inherited):
    cohort()
    manifest, sha = audit(tmp_path)
    if inherited == 'empty_host':
        monkeypatch.setitem(connection.settings_dict, 'HOST', '')
        monkeypatch.setenv('PGHOST', 'invalid.example.test')
    else:
        monkeypatch.setenv('PGSERVICE', 'untrusted-service')
    with pytest.raises(CommandError, match='explicit HOST'):
        apply(tmp_path, manifest, sha)


def test_effective_connected_identity_must_match_configuration(tmp_path, ready, monkeypatch):
    cohort()
    manifest, sha = audit(tmp_path)
    monkeypatch.setitem(connection.settings_dict, 'PORT', '99999')
    with pytest.raises(CommandError, match='Effective connection'):
        apply(tmp_path, manifest, sha)


@pytest.mark.parametrize('mode', ['environment', 'options', 'peer'])
def test_hostaddr_cannot_disguise_remote_connection_as_local(tmp_path, ready, monkeypatch, mode):
    from types import SimpleNamespace
    cohort()
    manifest, sha = audit(tmp_path)
    if mode == 'environment':
        monkeypatch.setenv('PGHOSTADDR', '192.0.2.1')
    elif mode == 'options':
        monkeypatch.setitem(connection.settings_dict, 'OPTIONS', {'hostaddr': '192.0.2.1'})
    else:
        cfg = connection.settings_dict
        monkeypatch.setattr(connection, 'connection', SimpleNamespace(info=SimpleNamespace(
            host=cfg['HOST'], port=int(cfg['PORT']), dbname=cfg['NAME'], hostaddr='192.0.2.1')))
    with pytest.raises(CommandError, match='Host-address overrides|loopback peer'):
        apply(tmp_path, manifest, sha)


def test_dry_run_does_not_abort_on_already_malformed_categories(tmp_path):
    """A `True` row with `{}` was malformed at audit time; the audit classifies it
    as a contradiction, so it is not a candidate and the dry-run reports cleanly."""
    PatientRecordFactory(no_pre_existing_conditions=True, preexisting_conditions={})
    manifest, sha = audit(tmp_path)

    repair(manifest, sha)  # must not raise "changed after the audit"


def test_apply_rolls_back_when_evidence_appears_during_the_loop(tmp_path, ready, monkeypatch):
    from omop_core.management.commands import repair_pre_existing_conditions as module

    ambiguous, *_ = cohort()
    manifest, sha = audit(tmp_path)
    original = module._flag_revisions
    calls = {'n': 0}

    def evidence_after_the_first_read(ids=None):
        calls['n'] += 1
        if calls['n'] == 1:
            return original(ids=ids)
        # A person answered "not none" after the audit: the post-write read must
        # see it and roll the whole apply back.
        return {ambiguous.pk: [{'changed_at': None, 'old': 'True', 'new': 'False'}]}

    monkeypatch.setattr(module, '_flag_revisions', evidence_after_the_first_read)
    with pytest.raises(CommandError, match='gained evidence during the run'):
        apply(tmp_path, manifest, sha)

    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True


def test_apply_aborts_when_a_legacy_attestation_becomes_explicit_after_the_audit(
        tmp_path, ready, monkeypatch):
    """At READ COMMITTED an in-place update of a legacy attestation takes no
    key-share lock on the person, so the repair must lock the attestation row and
    re-derive from it before deciding to delete it."""
    from omop_core.management.commands import repair_pre_existing_conditions as module

    ambiguous, *_ = cohort()
    legacy = PreexistingConditionsAttestation.objects.create(
        person=ambiguous.person, source='patient', confirmed_at=timezone.now())
    manifest, sha = audit(tmp_path)
    original = module.Command._lock

    def lock_after_the_confirmation(self, live, supplied, rolling_back):
        PreexistingConditionsAttestation.objects.filter(pk=legacy.pk).update(
            source='patient-explicit', confirmed_at=timezone.now())
        original(self, live, supplied, rolling_back)

    monkeypatch.setattr(module.Command, '_lock', lock_after_the_confirmation)
    with pytest.raises(CommandError, match='changed after the audit'):
        apply(tmp_path, manifest, sha)

    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True
    assert PreexistingConditionsAttestation.objects.filter(pk=legacy.pk).exists()


def test_apply_locks_the_candidates_attestations(tmp_path, ready):
    """The delete is only safe because the row is locked; assert the statement."""
    from django.test.utils import CaptureQueriesContext

    ambiguous, *_ = cohort()
    PreexistingConditionsAttestation.objects.create(
        person=ambiguous.person, source='patient', confirmed_at=timezone.now())
    manifest, sha = audit(tmp_path)

    with CaptureQueriesContext(connection) as captured:
        apply(tmp_path, manifest, sha)

    assert any(
        'FOR UPDATE' in query['sql']
        and 'preexisting_conditions_attestation' in query['sql']
        for query in captured.captured_queries)


def test_apply_aborts_when_the_locked_attestation_disappears_before_delete(
        tmp_path, ready, monkeypatch):
    from django.db.models import QuerySet

    ambiguous, *_ = cohort()
    PreexistingConditionsAttestation.objects.create(
        person=ambiguous.person, source='patient', confirmed_at=timezone.now())
    manifest, sha = audit(tmp_path)
    original = QuerySet.delete

    def refuse_attestation_delete(self, *args, **kwargs):
        if self.model is PreexistingConditionsAttestation:
            return 0, {}
        return original(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, 'delete', refuse_attestation_delete)
    with pytest.raises(CommandError, match='gained an attestation after the audit'):
        apply(tmp_path, manifest, sha)

    ambiguous.refresh_from_db()
    assert ambiguous.no_pre_existing_conditions is True


def test_a_write_to_an_unrelated_patient_after_the_audit_stales_the_approval(tmp_path):
    """The approval covers the whole table, not the cohort: the runbook's
    global write quiet from audit to apply exists because of this."""
    _ambiguous, _explicit, negative, _unknown = cohort()
    manifest, sha = audit(tmp_path)
    PatientRecord.objects.filter(pk=negative.pk).update(derived_at=timezone.now())

    with pytest.raises(CommandError, match='digest mismatch'):
        repair(manifest, sha)


def test_a_v2_manifest_is_refused_as_a_version_mismatch(tmp_path):
    """v2 hashed fewer per-record fields; an approved v2 audit must be
    regenerated, not matched against a v3 digest."""
    from omop_core.management.guarded_repair import approval_digest

    cohort()
    manifest, _sha = audit(tmp_path)
    payload = json.loads(open(manifest).read())
    payload['manifest_version'] = 2
    payload['approval_sha256'] = approval_digest(payload)
    old = tmp_path / 'v2.json'
    old.write_text(json.dumps(payload))

    with pytest.raises(CommandError, match='Audit version'):
        repair(str(old), payload['approval_sha256'])


def test_apply_has_no_per_candidate_record_lookup_before_classifying(tmp_path, ready):
    """The repair and the post-write read load the cohort with one query each
    instead of a single-row lookup per candidate. The write itself still does
    per-record work (refresh, snapshot, revisions); this pins only the lookups."""
    from django.test.utils import CaptureQueriesContext

    for _ in range(3):
        PatientRecordFactory(no_pre_existing_conditions=True, preexisting_conditions=[])
    manifest, sha = audit(tmp_path)
    with CaptureQueriesContext(connection) as captured:
        apply(tmp_path, manifest, sha)
    single = [query['sql'] for query in captured.captured_queries
              if 'FROM "patient_record"' in query['sql'] and 'LIMIT 1' in query['sql']]
    assert single == []

