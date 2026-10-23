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
    assert rows[authored.id]['classification'] == 'authored_true'
    assert rows[rewritten.id]['classification'] == 'rewritten_true'
    assert rows[pending.id]['classification'] == 'authored_true'
    assert rows[unattributed.id]['classification'] == 'unattributed_true'


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

    line = f'{record.id}|true||0||0\n'
    assert hashlib.sha256(line.encode()).hexdigest() == payload['manifest_sha256']


def test_summary_only_omits_rows_but_keeps_the_hash(tmp_path):
    _record(True, [])
    _record(None, [])

    full = _audit(tmp_path)
    summary = _audit(tmp_path / 'summary', '--summary-only')

    assert full['records'] and not summary['records']
    assert full['manifest_sha256'] == summary['manifest_sha256']
    assert full['totals'] == summary['totals']


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
