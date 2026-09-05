"""Copy the field-mapping curation from another PRomop instance into this one.

Curating the field-mapping screen is manual work — a reviewer picks an OMOP
concept for each PatientRecord field, bounds its answers, and approves it. This
copies the result of that work from an instance that already has it.

Usage::

    SOURCE_DATABASE_URL="postgresql://..." \\
      .venv/bin/python manage.py copy_field_mappings --dry-run
    SOURCE_DATABASE_URL="postgresql://..." \\
      .venv/bin/python manage.py copy_field_mappings

The destination is whatever ``DATABASE_URL`` points at, i.e. the instance you
would otherwise be running ``manage.py`` against. The source is opened as a
second connection and, on PostgreSQL, in a read-only session — this command
never writes to instance A.

Rows are matched on their natural key (``field_name``) and overwritten from the
source. Rows this instance has and the source does not are left alone unless
``--prune`` is given, which makes the local tables an exact mirror instead.

Everything happens in one transaction against the local database, so a failure
part-way leaves the curation as it was.
"""
import os

import dj_database_url
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from omop_core.mapping.field import (
    DEFAULT_TABLES,
    TABLES,
    apply_payload,
    read_payload,
)

# Named rather than 'source' so it cannot collide with an alias the project
# adds later for something else.
SOURCE_ALIAS = 'field_mapping_source'

_TABLE_LABELS = {
    'mappings': 'FieldConceptMapping',
    'custom_fields': 'CustomPatientField',
    'choices': 'FieldChoice (+ codes)',
    'formulas': 'FieldFormula',
    'synonyms': 'FieldSynonym',
}


# Django fills these in once, when it reads settings.DATABASES at startup
# (ConnectionHandler.configure_settings), and never again — so an alias added
# afterwards has to supply them itself or the first query dies on a missing
# key.  Mirrors that method's defaults.
_CONNECTION_DEFAULTS = {
    'ATOMIC_REQUESTS': False,
    'AUTOCOMMIT': True,
    'CONN_MAX_AGE': 0,
    'CONN_HEALTH_CHECKS': False,
    'TIME_ZONE': None,
    'OPTIONS': {},
}
_TEST_DEFAULTS = {
    'CHARSET': None, 'COLLATION': None, 'MIGRATE': True,
    'MIRROR': None, 'NAME': None,
}


def _apply_connection_defaults(config: dict) -> None:
    for key, value in _CONNECTION_DEFAULTS.items():
        config.setdefault(key, dict(value) if isinstance(value, dict) else value)
    for key in ('NAME', 'USER', 'PASSWORD', 'HOST', 'PORT'):
        config.setdefault(key, '')
    test = config.setdefault('TEST', {})
    for key, value in _TEST_DEFAULTS.items():
        test.setdefault(key, value)


def register_source_connection(url: str, alias: str = SOURCE_ALIAS) -> None:
    """Add ``url`` to the connection registry under ``alias``.

    Registered at runtime rather than in settings so the alias exists only for
    this command — a permanent entry would have the test runner trying to
    create a test database for instance A.
    """
    config = dj_database_url.parse(url, conn_max_age=0)
    if not config.get('NAME'):
        raise CommandError('Could not parse a database name out of the source URL.')
    _apply_connection_defaults(config)
    if 'postgresql' in config.get('ENGINE', ''):
        # Belt and braces: the command only issues SELECTs, and the server will
        # now reject anything else on this connection regardless.
        options = dict(config.get('OPTIONS') or {})
        options['options'] = '-c default_transaction_read_only=on'
        config['OPTIONS'] = options
    connections.databases[alias] = config


class Command(BaseCommand):
    help = (
        'Copy field mappings and synonyms '
        'from the instance at SOURCE_DATABASE_URL into this one.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-url',
            help='Source database URL. Defaults to $SOURCE_DATABASE_URL.',
        )
        parser.add_argument(
            '--tables', nargs='+', choices=TABLES, default=list(DEFAULT_TABLES),
            help=(
                'Which tables to copy. Default: mappings and synonyms. '
                'Related curation tables are available explicitly.'
            ),
        )
        parser.add_argument(
            '--prune', action='store_true',
            help=(
                'Also delete local rows the source does not have, mirroring it '
                'exactly. Off by default, so a copy is additive.'
            ),
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change and roll back.',
        )

    def handle(self, **options):
        url = options.get('source_url') or os.environ.get('SOURCE_DATABASE_URL')
        if not url:
            raise CommandError(
                'Set SOURCE_DATABASE_URL (or pass --source-url) to the database '
                'of the instance to copy field mappings from.'
            )
        tables = tuple(options['tables'])
        dry_run = options['dry_run']

        register_source_connection(url)
        try:
            payload = read_payload(SOURCE_ALIAS, tables=tables)
        except Exception as exc:
            raise CommandError(f'Could not read from the source database: {exc}')
        finally:
            connections[SOURCE_ALIAS].close()

        for table in tables:
            self.stdout.write(
                f'  read {len(payload.get(table, [])):4d}  {_TABLE_LABELS[table]}'
            )

        stats = apply_payload(
            payload, tables=tables, prune=options['prune'], dry_run=dry_run,
        )

        for warning in stats.warnings:
            self.stdout.write(self.style.WARNING(f'  ! {warning}'))

        self.stdout.write('')
        for table in tables:
            self.stdout.write(
                f'  {_TABLE_LABELS[table]:22s} '
                f'created {stats.created.get(table, 0):4d}  '
                f'updated {stats.updated.get(table, 0):4d}  '
                f'deleted {stats.deleted.get(table, 0):4d}  '
                f'skipped {stats.skipped.get(table, 0):4d}'
            )

        summary = (
            f'{stats.total(stats.created)} created, '
            f'{stats.total(stats.updated)} updated, '
            f'{stats.total(stats.deleted)} deleted'
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'Dry run — rolled back. Would have been: {summary}.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f'Copied field curation: {summary}.'))
