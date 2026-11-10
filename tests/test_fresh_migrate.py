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
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / 'omop_core' / 'data' / 'field_curation_v1.json'
# A full migrate, publisher catalogs included, takes about 90s locally. Well
# under the CI job's limit, so a hang ends here with migrate's output.
MIGRATE_TIMEOUT_S = 600


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


def test_migrate_builds_an_empty_database(empty_database):
    env = {**os.environ, 'DATABASE_URL': empty_database}
    # 0303 publishes to the broker on commit; a worker must not pick up a
    # request for a database that is about to be dropped. Blank rather than
    # unset, so load_dotenv() cannot bring a developer's .env value back.
    env['CELERY_BROKER_URL'] = ''
    env['CELERY_RESULT_BACKEND'] = ''

    result = subprocess.run(
        [sys.executable, 'manage.py', 'migrate', '--noinput'],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        timeout=MIGRATE_TIMEOUT_S,
    )

    output = (result.stdout + result.stderr)[-4000:]
    assert result.returncode == 0, f'migrate from empty failed:\n{output}'
    # 0240 seeded rather than skipped: earlier migrations create only some of
    # the fixture's mappings. If a later migration removes a fixture field on
    # purpose, drop it from this check.
    fixture_fields = {row['field_name'] for row in json.loads(FIXTURE.read_text())['mappings']}
    with psycopg.connect(empty_database) as conn:
        present = {name for (name,) in conn.execute('SELECT field_name FROM field_concept_mapping')}
    assert not fixture_fields - present, f'0240 did not seed: {sorted(fixture_fields - present)[:10]}'
