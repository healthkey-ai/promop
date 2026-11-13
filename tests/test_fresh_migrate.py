"""``migrate`` must build a database from empty (#2440).

pytest runs ``--no-migrations``, and the Django runner's full migrate builds a
``test_`` database, which 0240 and 0327 skip. So a data migration that reads a
live model ahead of its schema (0240 selected ``field_concept_mapping.tab``,
added by 0333) broke every fresh install and no suite saw it.

This runs the real ``manage.py migrate`` in a subprocess against a throwaway
database whose name does not start with ``test_``, so every data migration
runs as it does on a new instance. A subprocess rather than the executor on a
second connection: a migration that reaches for ``django.db.connection``
would otherwise read or write the test database instead.
"""
import importlib
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from django.apps import apps
from psycopg import sql

from omop_core.models import CustomPatientField, FieldConceptMapping

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / 'omop_core' / 'data' / 'field_curation_v1.json'
# A full migrate, publisher catalogs included, takes about 90s locally. Well
# under the CI job's limit, so a hang ends here with migrate's output.
MIGRATE_TIMEOUT_S = 600

# The columns the frozen writer in 0240 copies, plus the natural key.
_MAPPING_COLUMNS = (
    'field_name',
    'provenance', 'vocabulary_id', 'concept_code', 'unit', 'omop_table',
    'source_value', 'value_kind', 'type_concept_id', 'value_vocabulary',
    'multiple', 'status', 'reviewed_at', 'notes',
)
# Fixture keys 0240 resolves or derives rather than copying verbatim.
_RESOLVED_FIXTURE_KEYS = {
    'concept_id', 'concept_vocabulary_id', 'concept_code_resolved',
}

# cb-v2's 0240 seeds per table; the custom-field seeder is the branch under test.
seed_custom_fields = importlib.import_module(
    'omop_core.migrations.0240_seed_field_curation_fixture',
)._seed_custom_fields


def _server_url(database_url: str, name: str) -> str:
    return urlsplit(database_url)._replace(path=f'/{name}').geturl()


@pytest.fixture
def empty_database():
    """A new, empty database on the test server; dropped afterwards."""
    database_url = os.environ.get('DATABASE_URL', '')
    if not database_url.startswith(('postgres://', 'postgresql://')):
        pytest.skip('needs DATABASE_URL pointing at PostgreSQL')
    # Not test_*: the point is to run the migrations that skip test databases.
    name = f'promop_fresh_migrate_{uuid.uuid4().hex[:12]}'
    # template0, not template1: CI preinstalls extensions on template1, and a
    # new instance's database has none until the migrations create them.
    admin_url = _server_url(database_url, 'postgres')
    try:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)),
            )
    except psycopg.errors.InsufficientPrivilege:
        pytest.skip('the test role may not create databases')
    try:
        yield _server_url(database_url, name)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)),
            )


def _run_migrate(database_url: str, *target) -> str:
    """Run ``migrate`` (optionally to *target*) against *database_url*."""
    env = {**os.environ, 'DATABASE_URL': database_url}
    # 0303 publishes to the broker on commit; a worker must not pick up a
    # request for a database that is about to be dropped. Blank rather than
    # unset, so load_dotenv() cannot bring a developer's .env value back.
    env['CELERY_BROKER_URL'] = ''
    env['CELERY_RESULT_BACKEND'] = ''

    try:
        result = subprocess.run(
            [sys.executable, 'manage.py', 'migrate', *target, '--noinput'],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
            timeout=MIGRATE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        # TimeoutExpired still carries what was captured before the kill; a
        # report without it turns a diagnosable hang into "timed out".
        captured = exc.stdout or ''
        if isinstance(captured, bytes):
            captured = captured.decode(errors='replace')
        pytest.fail(
            f'migrate did not finish within {MIGRATE_TIMEOUT_S}s:\n{captured[-4000:]}',
        )

    output = (result.stdout + result.stderr)[-4000:]
    assert result.returncode == 0, f'migrate from empty failed:\n{output}'
    return output


def test_migrate_builds_an_empty_database(empty_database):
    # Migrate to 0239 first, so rows earlier migrations already seeded can be
    # told apart from the rows 0240 writes. Their values legitimately differ
    # from the fixture (0239 stamps reviewed_at with now()), and 0240 leaves
    # an existing natural key alone by design.
    _run_migrate(empty_database, 'omop_core', '0239_backfill_field_mapping_provenance')
    with psycopg.connect(empty_database) as conn:
        preexisting = {
            name for (name,) in conn.execute('SELECT field_name FROM field_concept_mapping')
        }

    _run_migrate(empty_database)

    # 0240 seeded rather than skipped: earlier migrations create only some of
    # the fixture's mappings. If a later migration removes a fixture field on
    # purpose, drop it from this check.
    fixture_rows = {
        row['field_name']: row
        for row in json.loads(FIXTURE.read_text())['mappings']
    }
    with psycopg.connect(empty_database) as conn:
        present = {name for (name,) in conn.execute('SELECT field_name FROM field_concept_mapping')}
        seeded = conn.execute(
            'SELECT ' + ', '.join(_MAPPING_COLUMNS)
            + ' FROM field_concept_mapping WHERE field_name = ANY(%s)',
            (list(fixture_rows),),
        ).fetchall()
    assert not set(fixture_rows) - present, (
        f'0240 did not seed: {sorted(set(fixture_rows) - present)[:10]}'
    )
    # Presence alone would pass a writer that drops a column or copies the wrong
    # source key, so compare the values 0240 writes for every row it created.
    mismatches = []
    for row in seeded:
        got = dict(zip(_MAPPING_COLUMNS, row))
        if got['field_name'] in preexisting:
            continue
        expected = fixture_rows[got['field_name']]
        for name in _MAPPING_COLUMNS:
            want = expected.get(name, '')
            if name == 'reviewed_at' and want:
                want = datetime.fromisoformat(want)
            if got[name] != want:
                mismatches.append((got['field_name'], name, got[name], want))
    assert not mismatches, (
        f'0240 seeded values that differ from the fixture: {mismatches[:5]}'
    )
    # Every fixture key is either copied by the frozen writer or deliberately
    # resolved/derived. A new key fails here instead of being dropped silently.
    fixture_keys = set().union(*(set(row) for row in fixture_rows.values()))
    unhandled = fixture_keys - set(_MAPPING_COLUMNS) - _RESOLVED_FIXTURE_KEYS
    assert not unhandled, f'unhandled fixture keys: {sorted(unhandled)}'


def test_a_hung_migrate_keeps_its_output(monkeypatch):
    """The timeout exists to report a *why*, so it must not discard stdout."""
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            args[0], kwargs.get('timeout'), output='Applying omop_core.0312_stuck ...',
        )

    monkeypatch.setattr(subprocess, 'run', fake_run)
    with pytest.raises(pytest.fail.Exception, match='0312_stuck'):
        _run_migrate('postgresql:///unused')


@pytest.mark.django_db
def test_an_existing_custom_field_is_left_alone_and_an_orphan_is_skipped():
    """The only new branch in 0240: existing custom fields are not overwritten."""
    mapping = FieldConceptMapping.objects.create(field_name='existing_mapping')
    created_field = CustomPatientField.objects.create(
        field_name='existing_field', display_name='Original',
        field_type='text', mode='editable', mapping=mapping,
    )
    before = (created_field.display_name, created_field.tab,
              created_field.field_type, created_field.mode)
    data = {
        'tables': ['custom_fields'],
        'custom_fields': [
            {   # already exists: must keep its values
                'field_name': 'existing_field',
                'mapping_field_name': 'existing_mapping',
                'display_name': 'Replaced', 'tab': 'labs',
                'field_type': 'number', 'mode': 'computed',
            },
            {   # no mapping in this database: skipped, not created
                'field_name': 'orphan_field',
                'mapping_field_name': 'missing_mapping',
                'display_name': 'Orphan', 'tab': 'labs',
                'field_type': 'text', 'mode': 'editable',
            },
        ],
    }

    created = seed_custom_fields(apps, data['custom_fields'])

    assert created == 0
    field = CustomPatientField.objects.get(field_name='existing_field')
    assert (field.display_name, field.tab, field.field_type, field.mode) == before
    assert not CustomPatientField.objects.filter(field_name='orphan_field').exists()
