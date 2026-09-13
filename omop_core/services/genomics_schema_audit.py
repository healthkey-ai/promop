"""Read-only deployment evidence for the shared genomic text columns.

Widths and aggregate counts describe the selected database at one snapshot.
They do not establish NOTE ownership, vocabulary validity or migration contents.
"""
from django.db import transaction
from psycopg import sql


TABLES = ('measurement', 'observation')
WIDTH = 60
MIGRATION = '0222_genomics_text_values'


def audit_schema(connection, *, statement_timeout_ms=30000):
    if connection.vendor != 'postgresql':
        raise ValueError('The genomics schema audit requires PostgreSQL.')
    if connection.in_atomic_block or not connection.get_autocommit():
        raise ValueError('Run the genomics schema audit outside an existing transaction.')
    if not 1 <= statement_timeout_ms <= 300000:
        raise ValueError('Statement timeout must be between 1 and 300000 milliseconds.')

    with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
        # Must precede every query, including metadata reads. The database
        # enforces read-only access and all counts share one consistent snapshot.
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(statement_timeout_ms)])
        cursor.execute('SELECT CURRENT_TIMESTAMP')
        captured_at = cursor.fetchone()[0].isoformat()
        columns = [_audit_column(cursor, table) for table in TABLES]
        cursor.execute("SELECT to_regclass('django_migrations')")
        history_available = cursor.fetchone()[0] is not None
        applied = None
        if history_available:
            cursor.execute('SELECT applied FROM django_migrations WHERE app = %s AND name = %s',
                           ['omop_core', MIGRATION])
            row = cursor.fetchone()
            applied = row[0].isoformat() if row else None

    return {
        'report_version': 1,
        'captured_at': captured_at,
        'expected_width': WIDTH,
        'columns_match': all(column['matches_expected_width'] for column in columns),
        'columns': columns,
        'migration_history': {
            'available': history_available, 'app': 'omop_core', 'name': MIGRATION, 'applied_at': applied,
            'historical_operations_verified': False,
        },
        'note_integrity_verified': False,
    }


def _audit_column(cursor, table):
    # Resolve the same relation the application's unqualified table name uses,
    # including installations whose search_path is not public.
    cursor.execute('''
        SELECT n.nspname, c.relname, t.typname, a.atttypmod
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE a.attrelid = to_regclass(%s) AND a.attname = 'value_as_string'
          AND NOT a.attisdropped
    ''', [table])
    row = cursor.fetchone()
    if row is None:
        return {'table': table, 'status': 'missing_column', 'matches_expected_width': False,
                'row_count': None, 'over_width_count': None, 'maximum_value_length': None}
    schema, relation, data_type, type_modifier = row
    max_length = type_modifier - 4 if data_type in ('varchar', 'bpchar') and type_modifier >= 4 else None
    result = {'table': table, 'schema': schema, 'data_type': data_type, 'max_length': max_length,
              'matches_expected_width': data_type == 'varchar' and max_length == WIDTH,
              'row_count': None, 'over_width_count': None, 'maximum_value_length': None}
    if data_type not in ('varchar', 'bpchar', 'text'):
        result['status'] = 'unsupported_type'
        return result
    query = sql.SQL('''
        SELECT count(*), count(*) FILTER (WHERE length(value_as_string) > %s),
               max(length(value_as_string)) FROM {}
    ''').format(sql.Identifier(schema, relation))
    cursor.execute(query, [WIDTH])
    result['row_count'], result['over_width_count'], result['maximum_value_length'] = cursor.fetchone()
    result['status'] = 'matches' if result['matches_expected_width'] else 'schema_mismatch'
    return result
