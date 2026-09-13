"""Audit real PostgreSQL schema variants without rewriting shared source values."""
import json
from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command, CommandError
from django.db import connection, DatabaseError, transaction

from omop_core.services.genomics_schema_audit import audit_schema


pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def schema():
    # Independent minimal tables exercise actual PostgreSQL types and prevent
    # schema-variant tests from altering the application's reflected tables.
    name = 'genomics_audit_' + uuid4().hex
    quoted = connection.ops.quote_name(name)
    with connection.cursor() as cursor:
        cursor.execute('SHOW search_path')
        previous = cursor.fetchone()[0]
        cursor.execute(f'CREATE SCHEMA {quoted}')
        cursor.execute("SELECT set_config('search_path', %s, false)", [name])
        for table in ('measurement', 'observation'):
            cursor.execute(f'CREATE TABLE {table} (value_as_string varchar(60))')
        cursor.execute('CREATE TABLE django_migrations (app text, name text, applied timestamptz)')
        cursor.execute("INSERT INTO django_migrations VALUES ('omop_core', '0222_genomics_text_values', '2026-09-12T10:51:35Z')")
    try:
        yield name
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('search_path', %s, false)", [previous])
            cursor.execute(f'DROP SCHEMA {quoted} CASCADE')


def command(**options):
    output = StringIO()
    call_command('audit_genomics_schema', environment='isolated-test', stdout=output, **options)
    return json.loads(output.getvalue())


def test_current_schema_reports_counts_and_recorded_history_without_source_text(schema):
    with connection.cursor() as cursor:
        cursor.executemany('INSERT INTO measurement VALUES (%s)', [('private source',), ('é' * 60,), (None,)])
    report = command(check=True)
    assert report['columns_match']
    assert report['report_version'] == 1
    assert report['environment'] == 'isolated-test'
    measurement, observation = report['columns']
    assert measurement['schema'] == schema
    assert measurement['max_length'] == 60
    assert measurement['row_count'] == 3
    assert measurement['over_width_count'] == 0
    assert measurement['maximum_value_length'] == 60  # characters, not bytes
    assert observation['row_count'] == 0
    assert observation['maximum_value_length'] is None
    assert report['migration_history']['applied_at'].startswith('2026-09-12T10:51:35')
    assert not report['migration_history']['historical_operations_verified']
    assert not report['note_integrity_verified']
    assert 'private source' not in json.dumps(report)


@pytest.mark.parametrize('column_type', ['text', 'varchar(10000)'])
def test_formerly_widened_columns_preserve_all_values_and_fail_check(schema, column_type):
    values = ['g' * 60, 'non-genomic ' + 'x' * 49, '源' * 10000, '[note:9999999999999999999]', None]
    with connection.cursor() as cursor:
        for table in ('measurement', 'observation'):
            cursor.execute(f'ALTER TABLE {table} ALTER COLUMN value_as_string TYPE {column_type}')
            cursor.executemany(f'INSERT INTO {table} VALUES (%s)', [(value,) for value in values])
    output = StringIO()
    with pytest.raises(CommandError, match='reconciliation review'):
        call_command('audit_genomics_schema', environment='isolated-test', check=True, stdout=output)
    report = json.loads(output.getvalue())
    assert not report['columns_match']
    for column in report['columns']:
        assert column['over_width_count'] == 2
        assert column['maximum_value_length'] == 10000
        assert column['status'] == 'schema_mismatch'
    # Retrying is safe; no NOTE rewrites, truncation, or interpretation of even
    # a missing reference-shaped value occurs in this diagnostic command.
    assert [c['over_width_count'] for c in command()['columns']] == [2, 2]
    with connection.cursor() as cursor:
        for table in ('measurement', 'observation'):
            cursor.execute(f'SELECT value_as_string FROM {table}')
            assert [row[0] for row in cursor.fetchall()] == values


def test_empty_widened_schema_still_requires_review(schema):
    with connection.cursor() as cursor:
        cursor.execute('ALTER TABLE measurement ALTER COLUMN value_as_string TYPE text')
    report = command()
    assert not report['columns_match']
    assert report['columns'][0]['row_count'] == 0
    assert report['columns'][0]['over_width_count'] == 0


def test_missing_or_unsupported_columns_cannot_look_verified(schema):
    with connection.cursor() as cursor:
        cursor.execute('DROP TABLE measurement')
        cursor.execute('ALTER TABLE observation ALTER COLUMN value_as_string TYPE integer USING NULL')
        cursor.execute('DROP TABLE django_migrations')
    report = command()
    assert not report['columns_match']
    assert [column['status'] for column in report['columns']] == ['missing_column', 'unsupported_type']
    assert all(column['row_count'] is None for column in report['columns'])
    assert not report['migration_history']['available']


def test_transaction_is_database_enforced_read_only_and_settings_are_restored(schema, monkeypatch):
    from omop_core.services import genomics_schema_audit
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        timeout = cursor.fetchone()[0]

    def accidental_write(cursor, conn, table):
        cursor.execute('SHOW transaction_isolation')
        assert cursor.fetchone()[0] == 'repeatable read'
        cursor.execute('SHOW transaction_read_only')
        assert cursor.fetchone()[0] == 'on'
        cursor.execute("INSERT INTO measurement VALUES ('must not persist')")

    monkeypatch.setattr(genomics_schema_audit, '_audit_column', accidental_write)
    with pytest.raises(DatabaseError):
        audit_schema(connection, statement_timeout_ms=1000)
    with connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM measurement')
        assert cursor.fetchone()[0] == 0
        cursor.execute('SHOW transaction_read_only')
        assert cursor.fetchone()[0] == 'off'
        cursor.execute('SHOW statement_timeout')
        assert cursor.fetchone()[0] == timeout


def test_query_failure_emits_no_partial_report_or_database_details(schema, monkeypatch):
    from omop_core.services import genomics_schema_audit

    def failed_query(*args):
        raise DatabaseError('private connection and patient details')

    monkeypatch.setattr(genomics_schema_audit, '_audit_column', failed_query)
    output = StringIO()
    with pytest.raises(CommandError, match=r'incomplete \(DatabaseError\)') as error:
        call_command('audit_genomics_schema', environment='test', stdout=output)
    assert output.getvalue() == ''
    assert 'private' not in str(error.value)


def test_nested_transaction_is_rejected_before_audit_queries(schema):
    with transaction.atomic(), pytest.raises(ValueError, match='outside an existing transaction'):
        audit_schema(connection)


def test_actual_statement_timeout_fails_without_partial_output(schema, monkeypatch):
    from omop_core.services import genomics_schema_audit

    def slow_query(cursor, conn, table):
        cursor.execute('SELECT pg_sleep(0.05)')

    monkeypatch.setattr(genomics_schema_audit, '_audit_column', slow_query)
    output = StringIO()
    with pytest.raises(CommandError, match='audit incomplete'):
        call_command('audit_genomics_schema', environment='test', statement_timeout_ms=1, stdout=output)
    assert output.getvalue() == ''
    with connection.cursor() as cursor:
        cursor.execute('SELECT count(*) FROM measurement')
        assert cursor.fetchone()[0] == 0


def test_invalid_timeout_and_unknown_database_are_rejected(schema):
    for timeout in (0, 300001):
        with pytest.raises(CommandError, match='Statement timeout'):
            command(statement_timeout_ms=timeout)
    with pytest.raises(CommandError, match='not configured'):
        command(database='not_configured')
