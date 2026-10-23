import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone

from omop_core.management.commands.repair_pre_existing_conditions import CONSTRAINT, REQUIRED
from omop_core.models import FieldFormula, PreexistingConditionsAttestation, RecordRevision
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


def test_formula_still_active_blocks_apply(tmp_path, ready):
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
    monkeypatch.setattr('omop_core.management.commands.repair_pre_existing_conditions.os.fsync', fail_flush)
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
    monkeypatch.setattr('omop_core.management.commands.repair_pre_existing_conditions.os.fsync', flush)
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
