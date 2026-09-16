"""Export the field-curation tables to a JSON fixture.

Writes the same payload that ``copy_curation`` transfers live between databases,
but to a file instead — useful for checking into version control so a fresh
instance can ``load_field_curation`` without a running source database.

Usage::

    # All five field-curation tables (no code_mappings):
    .venv/bin/python manage.py dump_field_curation --output omop_core/data/field_curation_v1.json

    # Specific tables:
    .venv/bin/python manage.py dump_field_curation --tables mappings synonyms

    # To stdout:
    .venv/bin/python manage.py dump_field_curation
"""
import json
import sys
from datetime import datetime, timezone

from django.core.management.base import BaseCommand

from omop_core.mapping.field import (
    TABLES,
    read_payload,
)

# Default tables for the fixture: everything except code_mappings, which steers
# ingest and is deployment-specific.
_FIXTURE_TABLES = ('mappings', 'custom_fields', 'choices', 'formulas', 'synonyms')

SCHEMA_VERSION = 1


class Command(BaseCommand):
    help = 'Export field-curation tables to a JSON fixture file.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--output', '-o',
            help='Output file path. Defaults to stdout.',
        )
        parser.add_argument(
            '--tables', nargs='+', choices=TABLES,
            default=list(_FIXTURE_TABLES),
            help=(
                'Which tables to export. Default: all five field-curation '
                'tables (no code_mappings).'
            ),
        )

    def handle(self, **options):
        tables = tuple(options['tables'])
        payload = read_payload(using='default', tables=tables)

        envelope = {
            'schema_version': SCHEMA_VERSION,
            'exported_at': datetime.now(timezone.utc).isoformat(),
            'source': 'dump_field_curation',
            'tables': list(tables),
            **payload,
        }

        output = json.dumps(envelope, indent=2, default=str, ensure_ascii=False)

        dest = options.get('output')
        if dest:
            with open(dest, 'w', encoding='utf-8') as f:
                f.write(output)
                f.write('\n')
            self.stdout.write(self.style.SUCCESS(
                f'Exported {len(tables)} table(s) to {dest}'
            ))
            for table in tables:
                rows = payload.get(table, [])
                self.stdout.write(f'  {table}: {len(rows)} row(s)')
        else:
            sys.stdout.write(output)
            sys.stdout.write('\n')
