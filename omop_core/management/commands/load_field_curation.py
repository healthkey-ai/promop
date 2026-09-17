"""Load a field-curation fixture exported by ``dump_field_curation``.

Seeds or updates the field-curation tables from a JSON file, using the same
``apply_payload`` logic that ``copy_curation`` uses for a live database
transfer. Idempotent: rows are matched on natural keys, so re-running updates
rather than duplicates.

Usage::

    # Dry run — report what would change:
    .venv/bin/python manage.py load_field_curation --input omop_core/data/field_curation_v1.json --dry-run

    # Apply:
    .venv/bin/python manage.py load_field_curation --input omop_core/data/field_curation_v1.json

    # From stdin:
    cat fixture.json | .venv/bin/python manage.py load_field_curation
"""
import json
import sys

from django.core.management.base import BaseCommand, CommandError

from omop_core.management.commands.dump_field_curation import SCHEMA_VERSION
from omop_core.mapping.field import (
    TABLES,
    apply_payload,
)

_TABLE_LABELS = {
    'mappings': 'FieldConceptMapping',
    'custom_fields': 'CustomPatientField',
    'choices': 'FieldChoice (+ codes)',
    'formulas': 'FieldFormula',
    'synonyms': 'FieldSynonym',
    'code_mappings': 'SourceCodeConceptMapping',
}


class Command(BaseCommand):
    help = 'Load a field-curation fixture from JSON.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--input', '-i',
            help='Input file path. Defaults to stdin.',
        )
        parser.add_argument(
            '--tables', nargs='+', choices=TABLES,
            help=(
                'Which tables to load. Default: all tables present in the '
                'fixture.'
            ),
        )
        parser.add_argument(
            '--prune', action='store_true',
            help=(
                'Delete local rows the fixture does not have, mirroring it '
                'exactly. Off by default, so a load is additive.'
            ),
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change and roll back.',
        )

    def handle(self, **options):
        src = options.get('input')
        if src:
            with open(src, encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = json.load(sys.stdin)

        if not isinstance(data, dict):
            raise CommandError('Fixture must be a JSON object.')

        version = data.get('schema_version')
        if version != SCHEMA_VERSION:
            raise CommandError(
                f'Unsupported schema_version {version!r} '
                f'(expected {SCHEMA_VERSION}).'
            )

        # Determine which tables to load: explicit --tables, or whatever the
        # fixture declares it exported.
        if options.get('tables'):
            tables = tuple(options['tables'])
        else:
            tables = tuple(data.get('tables', []))
        if not tables:
            raise CommandError(
                'No tables to load. The fixture has no "tables" key and '
                '--tables was not given.'
            )

        dry_run = options['dry_run']

        stats = apply_payload(
            data, tables=tables, prune=options['prune'], dry_run=dry_run,
        )

        for warning in stats.warnings:
            self.stdout.write(self.style.WARNING(f'  ! {warning}'))
        if stats.suppressed_warnings:
            self.stdout.write(self.style.WARNING(
                f'  ! ...and {stats.suppressed_warnings} more warnings.'
            ))

        self.stdout.write('')
        for table in tables:
            label = _TABLE_LABELS.get(table, table)
            self.stdout.write(
                f'  {label:22s} '
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
            self.stdout.write(self.style.SUCCESS(
                f'Loaded field curation: {summary}.'
            ))
