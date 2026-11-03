"""Audit or apply migration 0259 using the database's Athena SCCM mappings."""
import csv
import json
from contextlib import ExitStack

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from omop_core.data_migrations.athena_sccm_reconcile_v1 import REPORT_FIELDS, reconcile, summarize


class Command(BaseCommand):
    help = 'Reconcile approved ICD-10 mappings using Athena SCCM rows; read-only unless --apply.'

    def add_arguments(self, parser):
        parser.add_argument('vocabulary', nargs='?', choices=('ICD10', 'ICD10CM'))
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument('--apply', action='store_true')
        modes.add_argument('--dry-run', action='store_true')
        parser.add_argument('--database', default='default', choices=list(connections))
        parser.add_argument('--report', help='CSV file, or - for stdout')

    def handle(self, *args, **options):
        log = self.stderr if options['report'] == '-' else self.stdout
        with ExitStack() as stack:
            writer = None
            if options['report']:
                try:
                    stream = self.stdout if options['report'] == '-' else stack.enter_context(
                        open(options['report'], 'w', encoding='utf-8', newline=''))
                except OSError as exc:
                    raise CommandError(f'Cannot open report: {exc}') from exc
                writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
                writer.writeheader()

            def report(batch):
                if writer:
                    writer.writerows(batch)
                    stream.flush()

            log.write('Apply: SCCM definitions for future imports only' if options['apply']
                      else 'Dry run: no database writes')
            receipts = reconcile(apps, connections[options['database']], dry_run=not options['apply'],
                                 vocabularies=(options['vocabulary'],) if options['vocabulary']
                                 else ('ICD10', 'ICD10CM'), on_batch=report)
            log.write('Outcomes: ' + json.dumps(summarize(receipts), sort_keys=True))
