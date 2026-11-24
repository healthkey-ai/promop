"""Exporting the deletion log and replaying it after a backup restore
(patient_portal.erasure_replay, docs/member_erasure_architecture.md)."""
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from omop_core.models import PatientRecord, Person
from omop_oncology.models import Episode
from patient_portal import erasure, erasure_replay
from patient_portal import consent as svc
from patient_portal.models import AuditEvent, ConsentEvent, ConsentLedgerEntry, Identity, PatientUser
from tests.test_member_erasure import member  # noqa: F401 (fixture)
from tests.test_survey_record_mapping import mapped, patient  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def lost(kind, person, **extra):
    """The export line of an erasure the restored database knows nothing of:
    the person is here again, and the log row that said they were erased is
    not."""
    erasure._log(kind, person.person_id, 'system', **extra)
    lines = list(erasure_replay.export())
    AuditEvent.objects.filter(method='ERASE').delete()
    return lines


def replay(lines, **options):
    return erasure_replay.replay(erasure_replay.read(lines), **options)


def log():
    return [e.detail for e in AuditEvent.objects.filter(method='ERASE').order_by('id')]


def test_the_export_is_the_deletion_log_with_its_signatures(member):
    person_id, identity_id = member['person'].person_id, member['identity'].pk
    erasure.close_account(member['person'], member['identity'], actor=member['identity'])
    AuditEvent.objects.create(event_type=AuditEvent.EVENT_VIEW, method='GET', path='/api/v1/user/')

    lines = list(erasure_replay.export())

    assert len(lines) == 1
    line = json.loads(lines[0])
    assert line['detail'] == {'erasure': 'account', 'person_id': person_id, 'identity_id': identity_id}
    assert line['signature'] == AuditEvent.objects.get(method='ERASE').signature
    [entry] = erasure_replay.read(lines)
    assert entry.pk is None and entry.signature_valid()
    assert list(erasure_replay.export(since=timezone.now() + timedelta(minutes=1))) == []


def test_an_account_closed_after_the_backup_is_closed_again(member):
    person, identity = member['person'], member['identity']
    lines = lost('account', person, identity_id=identity.pk)
    ledger = ConsentLedgerEntry.objects.count()

    report = replay(lines)

    assert report.accounts == [person.person_id] and not report.skipped
    assert not Person.objects.filter(pk=person.pk).exists()
    assert not PatientRecord.objects.filter(person_id=person.pk).exists()
    assert not Identity.objects.filter(pk=identity.pk).exists()
    assert ConsentLedgerEntry.objects.count() == ledger
    # The erasure is logged as any other, and so is the fact it was a replay.
    assert [d['erasure'] for d in log()] == ['account', 'replay']
    assert log()[1]['replay_of'] == json.loads(lines[0])['signature']


def test_replaying_twice_does_nothing_the_second_time(member):
    lines = lost('record', member['person'], purpose='my_record')
    replay(lines)
    rows = AuditEvent.objects.count()
    record = PatientRecord.objects.get(person=member['person'])
    PatientRecord.objects.filter(pk=record.pk).update(stage='II')

    report = replay(lines)

    assert report.known == 1 and not report.records
    assert AuditEvent.objects.count() == rows
    assert PatientRecord.objects.get(pk=record.pk).stage == 'II'


def test_an_erasure_from_before_the_backup_is_not_replayed(member):
    """The restored database holds the entry: what it erased was erased, and
    what the member has entered since is theirs."""
    erasure._log('record', member['person'].person_id, 'system', purpose='my_record')

    report = replay(list(erasure_replay.export()))

    assert report.known == 1 and not report.records
    assert PatientRecord.objects.get(person=member['person']).stage == 'III'


def test_a_withdrawal_after_the_backup_is_put_back_and_the_record_erased(member):
    person = member['person']
    lines = lost('record', person, purpose='my_record')
    when = erasure_replay.read(lines)[0].timestamp

    report = replay(lines)

    assert report.records == [person.person_id]
    record = PatientRecord.objects.get(person=person)
    assert record.stage in (None, '') and not Episode.objects.filter(person=person).exists()
    assert PatientUser.objects.filter(person=person, identity=member['identity']).exists()
    latest = ConsentEvent.objects.filter(person=person, purpose__key='my_record').order_by('-recorded_at', '-id').first()
    assert (latest.decision, latest.source, latest.recorded_at) == (ConsentEvent.WITHDRAWN, ConsentEvent.SYSTEM, when)
    assert [d['erasure'] for d in log()] == ['record', 'replay']


def test_a_member_who_has_answered_the_permission_since_keeps_their_record(member):
    person = member['person']
    lines = lost('record', person, purpose='my_record')
    svc.record_consent(person, 'my_record', ConsentEvent.GRANTED, source=ConsentEvent.STAFF,
                       recorded_at=timezone.now() + timedelta(seconds=5))

    report = replay(lines)

    assert report.skipped == [(person.person_id, erasure_replay.ANSWERED)]
    assert PatientRecord.objects.get(person=person).stage == 'III' and log() == []


def test_an_account_made_after_the_erasure_holds_a_reused_id_and_is_left(member):
    person = member['person']
    lines = lost('account', person, identity_id=member['identity'].pk)
    PatientUser.objects.filter(person=person).update(created_at=timezone.now() + timedelta(minutes=1))

    report = replay(lines)

    assert report.skipped == [(person.person_id, erasure_replay.REUSED)]
    assert Person.objects.filter(pk=person.pk).exists() and log() == []


def test_a_person_with_no_sign_in_is_erased_only_when_asked(member):
    person = member['person']
    lines = lost('account', person)
    PatientUser.objects.filter(person=person).delete()

    assert replay(lines).skipped == [(person.person_id, erasure_replay.UNLINKED)]
    assert Person.objects.filter(pk=person.pk).exists()

    assert replay(lines, include_unlinked=True).accounts == [person.person_id]
    assert not Person.objects.filter(pk=person.pk).exists()
    # The sign-in was not named by the entry, so it was not closed with it.
    assert Identity.objects.filter(pk=member['identity'].pk).exists()


def test_a_person_who_is_not_in_the_restored_database_needs_nothing(member):
    lines = lost('account', member['person'])
    erasure.close_account(member['person'], member['identity'])
    rows = AuditEvent.objects.count()

    report = replay(lines)

    assert report.gone == 1 and not report.accounts and AuditEvent.objects.count() == rows


def test_a_dry_run_reports_and_erases_nothing(member, tmp_path, capsys):
    person = member['person']
    export = tmp_path / 'erasures.jsonl'
    export.write_text('\n'.join(lost('account', person, identity_id=member['identity'].pk)) + '\n')

    call_command('replay_erasures', str(export), '--dry-run')

    out = capsys.readouterr().out
    assert '1 account(s) would be closed again' in out and f'person {person.person_id}' in out
    assert Person.objects.filter(pk=person.pk).exists() and log() == []

    call_command('replay_erasures', str(export))
    assert not Person.objects.filter(pk=person.pk).exists()


@pytest.mark.parametrize('change', [
    lambda line: line['detail'].update(person_id=line['detail']['person_id'] + 1),
    lambda line: line.update(signature='0' * 64),
    lambda line: line.update(method='GET'),
    lambda line: line.pop('timestamp'),
])
def test_an_export_that_does_not_verify_replays_nothing(member, tmp_path, change):
    """Somebody else's id, a made-up signature, another kind of audit row or
    a broken line: the file is refused whole, the good lines with it."""
    person = member['person']
    good = lost('account', person, identity_id=member['identity'].pk)[0]
    bad = json.loads(good)
    change(bad)
    export = tmp_path / 'erasures.jsonl'
    export.write_text(good + '\n' + json.dumps(bad) + '\nnot json\n')

    with pytest.raises(CommandError, match='Nothing was replayed'):
        call_command('replay_erasures', str(export))

    assert Person.objects.filter(pk=person.pk).exists() and log() == []


def test_an_export_signed_under_another_key_is_refused(member, settings):
    lines = lost('account', member['person'])
    settings.AUDIT_HMAC_KEY = 'another-deployment'

    with pytest.raises(erasure_replay.BadExport):
        erasure_replay.read(lines)


def test_export_erasures_writes_the_file(member, tmp_path):
    erasure.close_account(member['person'], member['identity'])
    out = tmp_path / 'erasures.jsonl'

    call_command('export_erasures', '--output', str(out))

    assert len(erasure_replay.read(out.read_text().splitlines())) == 1
    with pytest.raises(CommandError):
        call_command('export_erasures', '--since', 'yesterday')


def test_entries_from_before_the_backup_time_are_not_looked_at(member, tmp_path):
    """An old entry the audit retention has since pruned is not in the
    restored database either; --since keeps the replay off it."""
    person = member['person']
    export = tmp_path / 'erasures.jsonl'
    export.write_text(lost('record', person, purpose='my_record')[0] + '\n')
    after = (timezone.now() + timedelta(minutes=1)).isoformat()

    call_command('replay_erasures', str(export), '--since', after)

    assert PatientRecord.objects.get(person=person).stage == 'III' and log() == []
    with pytest.raises(CommandError):
        call_command('replay_erasures', str(export), '--since', 'last week')
