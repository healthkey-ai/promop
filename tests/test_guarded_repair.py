"""The guards shared by the audit/repair command pairs (#2490).

The command tests exercise these through ``call_command``; this file pins the
shared module directly, so a change to it is caught whichever command it was
made for.
"""
import json
import os
import stat
from types import SimpleNamespace

import pytest
from django.core.management.base import CommandError
from django.db import connection

from omop_core.management import guarded_repair
from omop_core.management.commands import (
    audit_legacy_pregnancy_tobacco, audit_pre_existing_conditions,
    repair_legacy_pregnancy_tobacco, repair_pre_existing_conditions,
)
from omop_core.management.guarded_repair import (
    approval_digest, as_bool, canonical, digest, read_json, receipt,
    refuse_existing_manifest, target_guard, write_manifest, write_private,
)

pytestmark = pytest.mark.django_db


def _target_url(user='reader'):
    cfg = connection.settings_dict
    host = cfg['HOST']
    if ':' in host:  # an IPv6 literal needs brackets in a URL
        host = f'[{host}]'
    return f"postgresql://{user}@{host}:{cfg['PORT'] or 5432}/{cfg['NAME']}"


@pytest.fixture
def clean_env(monkeypatch):
    for name in ('PROD_DATABASE_URL', 'STAGING_DATABASE_URL', 'PGSERVICE', 'PGHOSTADDR'):
        monkeypatch.delenv(name, raising=False)


# --- target guard -----------------------------------------------------------

def test_local_loopback_target_is_accepted(clean_env):
    target_guard('local')


def test_staging_is_accepted_only_on_an_exact_staging_match(clean_env, monkeypatch):
    with pytest.raises(CommandError, match='STAGING_DATABASE_URL'):
        target_guard('staging')
    monkeypatch.setenv('STAGING_DATABASE_URL', _target_url() + '_other')
    with pytest.raises(CommandError, match='STAGING_DATABASE_URL'):
        target_guard('staging')
    monkeypatch.setenv('STAGING_DATABASE_URL', _target_url())
    target_guard('staging')


@pytest.mark.parametrize('environment', ['local', 'staging'])
def test_production_target_is_refused_whatever_the_environment(clean_env, monkeypatch, environment):
    # Credentials are not part of the identity: another user on the production
    # database is still production. Production wins over a matching staging URL.
    monkeypatch.setenv('PROD_DATABASE_URL', _target_url(user='someone-else'))
    monkeypatch.setenv('STAGING_DATABASE_URL', _target_url())
    with pytest.raises(CommandError, match='Production writes are forbidden'):
        target_guard(environment)


@pytest.mark.parametrize('inherited', ['empty_host', 'pgservice', 'service_option'])
def test_implicit_connection_targets_are_refused(clean_env, monkeypatch, inherited):
    if inherited == 'empty_host':
        monkeypatch.setitem(connection.settings_dict, 'HOST', '')
    elif inherited == 'pgservice':
        monkeypatch.setenv('PGSERVICE', 'untrusted-service')
    else:
        monkeypatch.setitem(connection.settings_dict, 'OPTIONS',
                            {**connection.settings_dict.get('OPTIONS', {}), 'service': 'x'})
    with pytest.raises(CommandError, match='explicit HOST'):
        target_guard('local')


@pytest.mark.parametrize('source', ['environment', 'options'])
def test_host_address_overrides_are_refused(clean_env, monkeypatch, source):
    if source == 'environment':
        monkeypatch.setenv('PGHOSTADDR', '192.0.2.1')
    else:
        monkeypatch.setitem(connection.settings_dict, 'OPTIONS',
                            {**connection.settings_dict.get('OPTIONS', {}), 'hostaddr': '192.0.2.1'})
    with pytest.raises(CommandError, match='Host-address overrides'):
        target_guard('local')


def test_effective_connection_must_match_the_configured_target(clean_env, monkeypatch):
    connection.ensure_connection()
    monkeypatch.setitem(connection.settings_dict, 'PORT', '99999')
    with pytest.raises(CommandError, match='Effective connection'):
        target_guard('local')


def test_local_requires_a_loopback_peer(clean_env, monkeypatch):
    cfg = connection.settings_dict
    monkeypatch.setattr(connection, 'connection', SimpleNamespace(info=SimpleNamespace(
        host=cfg['HOST'], port=int(cfg['PORT'] or 5432), dbname=cfg['NAME'],
        hostaddr='192.0.2.1')))
    with pytest.raises(CommandError, match='loopback peer'):
        target_guard('local')


def test_local_requires_a_configured_loopback_host(clean_env, monkeypatch):
    # A remote host whose peer happens to be loopback (a tunnel, a proxy) is
    # still not a configured local database.
    cfg = connection.settings_dict
    monkeypatch.setitem(cfg, 'HOST', 'db.example.test')
    monkeypatch.setattr(connection, 'connection', SimpleNamespace(info=SimpleNamespace(
        host='db.example.test', port=int(cfg['PORT'] or 5432), dbname=cfg['NAME'],
        hostaddr='127.0.0.1')))
    with pytest.raises(CommandError, match='explicitly configured loopback'):
        target_guard('local')


# --- private files ----------------------------------------------------------

def test_write_private_is_owner_only_canonical_and_never_overwrites(tmp_path):
    path = tmp_path / 'backup.json'
    write_private(str(path), {'b': 1, 'a': [1, 2]})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text() == canonical({'a': [1, 2], 'b': 1}) + '\n'
    assert read_json(str(path)) == {'a': [1, 2], 'b': 1}
    with pytest.raises(CommandError, match='exclusively create'):
        write_private(str(path), {'replaced': True})
    assert read_json(str(path)) == {'a': [1, 2], 'b': 1}


def test_write_manifest_is_owner_only_and_never_overwrites(tmp_path):
    path = tmp_path / 'new-dir' / 'audit.json'
    refuse_existing_manifest(None)
    refuse_existing_manifest(str(path))
    write_manifest(str(path), {'records': []})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {'records': []}
    with pytest.raises(CommandError, match='refusing to overwrite'):
        refuse_existing_manifest(str(path))
    with pytest.raises(CommandError, match='refusing to overwrite'):
        write_manifest(str(path), {'records': ['replaced']})
    assert json.loads(path.read_text()) == {'records': []}


@pytest.mark.parametrize('content', ['[]', 'not json'])
def test_read_json_accepts_only_an_object(tmp_path, content):
    path = tmp_path / 'input.json'
    path.write_text(content)
    with pytest.raises(CommandError, match='JSON object'):
        read_json(str(path))


# --- digests ----------------------------------------------------------------

def test_receipt_is_signed_over_everything_but_its_signature():
    payload = receipt('repair', 'run', {'name': 'db'}, 'abc', [{'id': 1}])
    unsigned = {key: value for key, value in payload.items() if key != 'backup_sha256'}
    assert payload['backup_sha256'] == digest(unsigned)


def test_approval_digest_ignores_only_the_timestamp_and_itself():
    payload = {'generated_at': 'now', 'records': [1], 'approval_sha256': 'x'}
    expected = approval_digest(payload)
    assert approval_digest({**payload, 'generated_at': 'later', 'approval_sha256': 'y'}) == expected
    assert approval_digest({**payload, 'records': [2]}) != expected


@pytest.mark.parametrize('text, expected', [
    (None, None), ('', None), ('  ', None), ('maybe', None),
    (' Yes ', True), ('t', True), ('1', True), ('ON', True),
    ('no', False), ('F', False), ('0', False), ('off', False),
])
def test_as_bool(text, expected):
    assert as_bool(text) is expected


def test_every_audit_repair_command_uses_the_shared_guards():
    for module in (repair_legacy_pregnancy_tobacco, repair_pre_existing_conditions):
        for name in ('target_guard', 'write_private', 'read_json', 'canonical', 'digest',
                     'receipt', 'lock_rows', 'pin_transaction_isolation'):
            assert getattr(module, name) is getattr(guarded_repair, name), (module, name)
    for module in (audit_legacy_pregnancy_tobacco, audit_pre_existing_conditions):
        for name in ('approval_digest', 'as_bool', 'make_transaction_read_only',
                     'refuse_existing_manifest', 'write_manifest'):
            assert getattr(module, name) is getattr(guarded_repair, name), (module, name)
    # The apply path stays at READ COMMITTED: a serializable snapshot taken by
    # the audit read cannot see evidence committed before the locks, which is
    # the window the post-lock re-read exists to close.
    assert not hasattr(guarded_repair, 'make_transaction_serializable')


def test_pin_transaction_isolation_chooses_by_apply_and_ownership(monkeypatch):
    calls = []
    monkeypatch.setattr(guarded_repair, 'make_transaction_read_committed',
                        lambda: calls.append('read-committed'))
    monkeypatch.setattr(guarded_repair, 'make_transaction_read_only',
                        lambda: calls.append('read-only'))

    guarded_repair.pin_transaction_isolation(applying=True, owns=False)
    guarded_repair.pin_transaction_isolation(applying=False, owns=False)
    assert calls == []

    guarded_repair.pin_transaction_isolation(applying=True, owns=True)
    guarded_repair.pin_transaction_isolation(applying=False, owns=True)
    assert calls == ['read-committed', 'read-only']


@pytest.mark.django_db(transaction=True)
def test_make_transaction_read_committed_issues_the_statement():
    from django.db import transaction
    from django.test.utils import CaptureQueriesContext

    with transaction.atomic():
        with CaptureQueriesContext(connection) as captured:
            guarded_repair.make_transaction_read_committed()

    assert any('READ COMMITTED' in query['sql'].upper()
               for query in captured.captured_queries)


def test_lock_rows_locks_the_given_rows_instead_of_the_table():
    """A table lock blocked every patient write for the repair's whole loop."""
    from django.db import transaction
    from django.test.utils import CaptureQueriesContext

    from omop_core.models import PatientRecord
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory()
    with transaction.atomic():
        with CaptureQueriesContext(connection) as captured:
            guarded_repair.lock_rows(PatientRecord, [record.pk])
    statements = [query['sql'] for query in captured.captured_queries]
    assert any('FOR UPDATE' in sql for sql in statements)
    assert not any('LOCK TABLE' in sql for sql in statements)
    assert any('FOR UPDATE' in sql and str(record.pk) in sql for sql in statements)


def test_lock_rows_translates_a_lock_timeout(monkeypatch):
    from unittest import mock

    from django.db import OperationalError

    from omop_core.models import PatientRecord

    timeout = OperationalError('canceling statement due to lock timeout')
    with mock.patch.object(PatientRecord.objects, 'select_for_update', side_effect=timeout):
        with pytest.raises(CommandError, match="Could not acquire the cohort's row locks"):
            guarded_repair.lock_rows(PatientRecord, [1, 2])


def test_lock_rows_with_no_candidates_does_not_lock_anything():
    from django.db import transaction
    from django.test.utils import CaptureQueriesContext

    from omop_core.models import PatientRecord

    with transaction.atomic():
        with CaptureQueriesContext(connection) as captured:
            guarded_repair.lock_rows(PatientRecord, [])
    assert not [query for query in captured.captured_queries
                if 'FOR UPDATE' in query['sql']]


def test_lock_rows_restores_the_previous_lock_timeout():
    """`SET LOCAL lock_timeout = 0` after acquisition meant "no limit" for the
    writes, dropping a role/database/session timeout instead of restoring it."""
    from django.db import transaction

    from omop_core.models import PatientRecord
    from tests.factories import PatientRecordFactory

    record = PatientRecordFactory()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '7s'")
        guarded_repair.lock_rows(PatientRecord, [record.pk])
        with connection.cursor() as cursor:
            cursor.execute('SHOW lock_timeout')
            assert cursor.fetchone()[0] == '7s'
