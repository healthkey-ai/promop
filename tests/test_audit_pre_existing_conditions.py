"""The provenance audit for the pre-existing-conditions answer (#1988).

The audit exists to make one thing honest: the database cannot prove whether a stored
``True`` was written by the retired formula or by a person. These tests pin the
classification rules, the evidence the manifest hashes, and the read-only guarantee the
repair command will rely on.
"""

import hashlib
import json
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from omop_core.models import RecordRevision
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


def _record(flag=None, categories=None, edited=None):
    return PatientRecordFactory(
        no_pre_existing_conditions=flag,
        preexisting_conditions=categories,
        user_edited_fields=edited or [],
    )


def _revision(record, new_value, old_value='None', changed_by='7'):
    return RecordRevision.objects.create(
        patient_record=record, field='no_pre_existing_conditions',
        old_value=old_value, new_value=new_value, changed_by=changed_by)


def _audit(path, *args):
    if path.is_dir():
        path = path / 'manifest.json'
    call_command('audit_pre_existing_conditions', '--output', str(path), *args,
                 stdout=StringIO())
    return json.loads(path.read_text())


def _by_id(payload):
    return {row['id']: row for row in payload['records']}


def test_every_evidence_shape_gets_its_own_classification(tmp_path):
    unanswered = _record(None, [])
    false_answer = _record(False, [])
    contradiction = _record(True, ['infections'])
    authored = _record(True, [])
    _revision(authored, 'True')
    rewritten = _record(True, [])
    _revision(rewritten, 'False', old_value='True')
    pending = _record(True, [], edited=['no_pre_existing_conditions'])
    unattributed = _record(True, [])

    rows = _by_id(_audit(tmp_path))

    assert rows[unanswered.id]['classification'] == 'unanswered'
    assert rows[false_answer.id]['classification'] == 'explicit_false'
    assert rows[contradiction.id]['classification'] == 'contradiction'
    assert rows[authored.id]['classification'] == 'unattributed_true'
    assert rows[rewritten.id]['classification'] == 'rewritten_true'
    assert rows[pending.id]['classification'] == 'unattributed_true'
    assert rows[unattributed.id]['classification'] == 'unattributed_true'


@pytest.mark.parametrize('source,expected', [
    ('patient', 'unattributed_true'), ('clinician', 'unattributed_true'),
    ('patient-explicit', 'authored_true'), ('clinician-explicit', 'authored_true'),
    ('host-explicit', 'authored_true'),
])
def test_only_explicit_attestation_is_evidence_of_an_authored_answer(tmp_path, source, expected):
    from omop_core.models import PreexistingConditionsAttestation
    from django.utils import timezone

    record = _record(True, [])
    PreexistingConditionsAttestation.objects.create(
        person=record.person, confirmed_at=timezone.now(), source=source)

    rows = _by_id(_audit(tmp_path))

    assert rows[record.id]['classification'] == expected
    assert rows[record.id]['attested'] is (expected == 'authored_true')


def test_a_false_answer_is_never_treated_as_a_formula_output(tmp_path):
    """The retired formula only produced True or None, so False must be kept."""
    false_answer = _record(False, [])

    payload = _audit(tmp_path)

    assert _by_id(payload)[false_answer.id]['recommended_action'] == 'keep'


def test_the_summary_counts_every_record_once(tmp_path):
    _record(True, [])
    _record(True, [])
    _record(None, [])
    _record(True, ['hypertension'])

    totals = _audit(tmp_path)['totals']

    assert totals['records'] == 4
    assert sum(totals['by_classification'].values()) == 4
    assert totals['by_classification']['unattributed_true'] == 2
    assert totals['by_classification']['contradiction'] == 1
    assert totals['by_classification']['unanswered'] == 1
    assert totals['by_recommended_action']['needs_policy'] == 2


def test_the_manifest_pins_the_data_and_the_evidence(tmp_path):
    record = _record(True, [])
    digest = _audit(tmp_path / 'first.json')['manifest_sha256']

    # Same data and evidence -> the same manifest.
    assert _audit(tmp_path / 'again.json')['manifest_sha256'] == digest

    # A changed category invalidates it.
    record.preexisting_conditions = ['infections']
    record.save(update_fields=['preexisting_conditions'])
    assert _audit(tmp_path / 'changed.json')['manifest_sha256'] != digest

    # So does new provenance evidence, even when the stored value is unchanged.
    record.preexisting_conditions = []
    record.save(update_fields=['preexisting_conditions'])
    assert _audit(tmp_path / 'restored.json')['manifest_sha256'] == digest
    _revision(record, 'True')
    assert _audit(tmp_path / 'revision.json')['manifest_sha256'] != digest


def test_the_manifest_hash_is_recomputable_from_the_records(tmp_path):
    record = _record(True, [])
    payload = _audit(tmp_path)

    line = json.dumps({
        'id': record.id, 'flag': True, 'categories_raw': [],
        'flag_history': [], 'pending_edit': False, 'attestation': None,
    }, sort_keys=True, separators=(',', ':')) + '\n'
    assert payload['manifest_version'] == 2
    assert hashlib.sha256(line.encode()).hexdigest() == payload['manifest_sha256']


def test_manifest_pins_raw_categories_and_attestation_metadata(tmp_path):
    from django.utils import timezone
    from omop_core.models import PreexistingConditionsAttestation

    record = _record(True, None)
    missing = _audit(tmp_path / 'missing.json')['manifest_sha256']
    record.preexisting_conditions = []
    record.save(update_fields=['preexisting_conditions'])
    empty = _audit(tmp_path / 'empty.json')['manifest_sha256']
    assert empty != missing
    row = PreexistingConditionsAttestation.objects.create(
        person=record.person, source='patient', confirmed_at=timezone.now())
    legacy = _audit(tmp_path / 'legacy.json')['manifest_sha256']
    row.source = 'patient-explicit'
    row.save(update_fields=['source'])
    explicit = _audit(tmp_path / 'explicit.json')['manifest_sha256']
    assert explicit != legacy
    row.confirmed_at += timezone.timedelta(seconds=1)
    row.save(update_fields=['confirmed_at'])
    assert _audit(tmp_path / 'renewed.json')['manifest_sha256'] != explicit


def test_summary_only_omits_rows_but_keeps_the_hash(tmp_path):
    _record(True, [])
    _record(None, [])

    full = _audit(tmp_path)
    summary = _audit(tmp_path / 'summary', '--summary-only')

    assert full['records'] and not summary['records']
    assert full['manifest_sha256'] == summary['manifest_sha256']
    assert full['totals'] == summary['totals']
    assert full['approval_sha256'] != summary['approval_sha256']


def test_approval_hash_covers_reviewed_rows_but_not_generation_time(tmp_path):
    from omop_core.management.commands.audit_pre_existing_conditions import approval_digest
    _record(True, [])
    payload = _audit(tmp_path)
    expected = payload['approval_sha256']
    payload['generated_at'] = 'different generation time'
    assert approval_digest(payload) == expected
    payload['records'][0]['classification'] = 'authored_true'
    assert approval_digest(payload) != expected


def test_the_command_refuses_to_overwrite_an_existing_manifest(tmp_path):
    output = tmp_path / 'manifest.json'
    output.write_text('{}')

    with pytest.raises(CommandError):
        call_command('audit_pre_existing_conditions', '--output', str(output),
                     stdout=open('/dev/null', 'w'))


def test_fail_on_gates_a_classification(tmp_path):
    _record(True, [])

    with pytest.raises(CommandError, match='unattributed_true'):
        call_command('audit_pre_existing_conditions', '--fail-on',
                     'unattributed_true', stdout=open('/dev/null', 'w'))


def test_the_manifest_is_owner_only(tmp_path):
    """It lists person ids and their categories, like the repair backup."""
    _record(flag=True)
    path = tmp_path / 'manifest.json'
    _audit(path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_manifest_that_appears_mid_run_is_refused_cleanly(tmp_path, monkeypatch):
    """The exists check and the exclusive create race; losing it is a CommandError."""
    import os
    _record(flag=True)
    path = tmp_path / 'manifest.json'
    real_open = os.open

    def racing_open(target, flags, *args):
        if str(target) == str(path):
            path.write_text('{}')
        return real_open(target, flags, *args)

    monkeypatch.setattr(os, 'open', racing_open)
    with pytest.raises(CommandError, match='already exists'):
        call_command('audit_pre_existing_conditions', '--output', str(path), stdout=StringIO())
    assert path.read_text() == '{}'


def _django_migrations(names):
    """A django_migrations table holding exactly these omop_core names.

    pytest runs with --no-migrations, so the table may not exist; the test's
    transaction rolls back whatever is created or written here.
    """
    from django.db import connection
    from django.db.migrations.recorder import MigrationRecorder

    recorder = MigrationRecorder(connection)
    recorder.ensure_schema()
    recorder.migration_qs.filter(app='omop_core').delete()
    for name in names:
        recorder.record_applied('omop_core', name)


def test_other_0304_0305_migrations_do_not_count_as_the_guard(tmp_path):
    """`dev` has other 0304_*/0305_* migrations; a prefix match reported the
    #1989 guard as applied when it was not (#2489)."""
    _django_migrations([
        '0304_axis_variant_refresh_progress_at',
        '0305_merge_20261025_0053',
        '0305_reverse_suggest_decline',
    ])

    formula = _audit(tmp_path / 'manifest.json')['formula']

    assert formula['migration_0304_applied'] is False
    assert formula['migration_0305_applied'] is False


def test_the_guard_migrations_are_recognised_by_name(tmp_path):
    _django_migrations([
        '0304_deactivate_pre_existing_condition_formula',
        '0305_patientrecord_pre_existing_flag_guard',
    ])

    formula = _audit(tmp_path / 'manifest.json')['formula']

    assert formula['migration_0304_applied'] is True
    assert formula['migration_0305_applied'] is True
