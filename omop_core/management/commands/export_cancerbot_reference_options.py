"""Export only the allowlisted reference providers needed by the field inventory."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg import sql
from django.core.management.base import BaseCommand, CommandError

from omop_core.services.cancerbot_reference_options import (
    REFERENCE_COLUMNS, REFERENCE_MODELS, ReferenceOptions, SOURCE_SHA256, validate_reference_tables,
)
from omop_core.services.field_inventory import source_revision, validate_live_export


def read_reference_snapshot(conn):
    tables = sorted('trials_' + name.lower() for name in REFERENCE_MODELS)
    conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    conn.execute("SET LOCAL statement_timeout = '45s'")
    metadata = conn.execute(
        'SELECT table_name, column_name FROM information_schema.columns '
        'WHERE table_schema = %s AND table_name = ANY(%s) ORDER BY table_name, ordinal_position',
        ('public', tables),
    ).fetchall()
    columns = {table: [column for t, column in metadata if t == table and column in REFERENCE_COLUMNS] for table in tables}
    if any('id' not in columns[table] for table in tables):
        raise ValueError('One or more allowlisted reference tables are missing or inaccessible.')
    data = {}
    for table in tables:
        query = sql.SQL('SELECT {} FROM {}.{} ORDER BY {}').format(
            sql.SQL(', ').join(map(sql.Identifier, columns[table])),
            sql.Identifier('public'), sql.Identifier(table), sql.Identifier('id'))
        data[table] = [dict(zip(columns[table], row)) for row in conn.execute(query).fetchall()]
    return {'exported_at': datetime.now(timezone.utc).isoformat(),
            'transaction_read_only': conn.execute('SHOW transaction_read_only').fetchone()[0], 'tables': data}


def build_reference_export(manifest, source, snapshot):
    if snapshot.get('transaction_read_only') != 'on':
        raise ValueError('Reference snapshot must record a read-only transaction.')
    validate_reference_tables(snapshot['tables'])
    definition = snapshot.get('source_definition', {})
    if not definition.get('revision') or definition.get('files', {}).get('trials/services/value_options.py') != SOURCE_SHA256:
        raise ValueError('Reference snapshot requires the reviewed provider source revision and hash.')
    provider = ReferenceOptions(source, snapshot['tables'])
    options = {}
    for binding in manifest['cancerbot_bindings']:
        # A repeated export refreshes existing live references as well.
        if binding['coverage'] in {'requires_live_export', 'staging_catalog_available_context_pending', 'covered_by_live_export', 'covered_by_staging_reference'}:
            options[binding['option_list']] = provider.public_options(binding['expression'])
    payload = {'schema_version': 1, 'source_revision': definition['revision'],
               'exported_at': snapshot['exported_at'], 'options': options}
    validate_live_export(payload, [b['option_list'] for b in manifest['cancerbot_bindings']])
    return payload


class Command(BaseCommand):
    help = 'Build CancerBot option lists from allowlisted reference tables; no patient or trial rows are read.'

    def add_arguments(self, parser):
        parser.add_argument('--inventory', type=Path, required=True)
        parser.add_argument('--cancerbot-root', type=Path, required=True)
        parser.add_argument('--snapshot', type=Path, help='Replay a previously captured reference snapshot without a database.')
        parser.add_argument('--snapshot-output', type=Path)
        parser.add_argument('--output', type=Path, required=True)

    def handle(self, **options):
        manifest = json.loads(options['inventory'].read_text())
        source_path = options['cancerbot_root'] / 'trials/services/value_options.py'
        source = source_path.read_text()
        ReferenceOptions(source, {})  # Verify reviewed provider source before any connection.
        if options['snapshot']:
            snapshot = json.loads(options['snapshot'].read_text())
        else:
            dsn = os.environ.get('CANCERBOT_DATABASE_URL')
            if not dsn:
                raise CommandError('Set CANCERBOT_DATABASE_URL privately, or supply --snapshot.')
            try:
                with psycopg.connect(dsn, connect_timeout=15, options='-c default_transaction_read_only=on') as conn:
                    snapshot = read_reference_snapshot(conn)
            except psycopg.Error as exc:
                raise CommandError(f'Reference connection/query failed: {type(exc).__name__} ({exc.sqlstate or "connection"}).') from None
        if not options['snapshot']:
            snapshot['source_definition'] = source_revision(options['cancerbot_root'], ['trials/services/value_options.py', 'trials/models.py'])
        try:
            payload = build_reference_export(manifest, source, snapshot)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        snapshot['provider_source_sha256'] = SOURCE_SHA256
        snapshot['provider_source'] = source
        snapshot['semantics'] = 'Live reference rows interpreted with checked-in provider definitions; deployed application revision is not asserted.'
        if options['snapshot_output']:
            options['snapshot_output'].write_text(json.dumps(snapshot, indent=2, ensure_ascii=False, default=str) + '\n')
        options['output'].write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + '\n')
        self.stdout.write(json.dumps({'public_lists': len(payload['options']),
                                     'reference_tables': len(snapshot['tables'])}))
