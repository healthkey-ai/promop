"""Reconcile mapped standard destination domains against the live Athena website."""
import csv
import math
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.athena_domain_reconciliation import (
    AthenaDomainBrowser, destination_snapshots, external, reconcile_domain,
)

REPORT_FIELDS = ('source_vocabulary', 'concept_id', 'vocabulary_id', 'concept_code', 'local_domain',
                 'athena_domain', 'outcome', 'reason', 'stale_mappings', 'reference', 'checked_at')
UNRESOLVED = {'lookup_failed', 'identity_conflict', 'upstream_nonstandard', 'upstream_inactive',
              'missing_domain', 'changed_during_lookup', 'unsupported_domain'}


class Command(BaseCommand):
    help = 'Check standard destination domains against live Athena for a source vocabulary; default is dry-run.'

    def add_arguments(self, parser):
        parser.add_argument('source_vocabulary', help='Exact stored source vocabulary, e.g. ICD10 or ICD10CM')
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true', help='Update only concept.domain_id when Athena differs.')
        mode.add_argument('--dry-run', action='store_true', help='Check/report without writes (default).')
        parser.add_argument('--database', default='default', choices=list(connections))
        parser.add_argument('--report', help='Per-destination CSV file, or - for CSV on stdout')
        parser.add_argument('--delay', type=float, default=1, help='Minimum seconds between Athena page visits')
        parser.add_argument('--timeout', type=float, default=30, help='Seconds per page/response')
        parser.add_argument('--batch-size', type=int, default=25, help='Concepts per browser session (1–100)')
        parser.add_argument('--browser-executable', help='Optional Chrome/Chromium executable')
        parser.add_argument('--browser-storage-state', help='Optional Playwright storage-state file')

    def handle(self, *args, **options):
        if (not math.isfinite(options['delay']) or options['delay'] < 0
                or not math.isfinite(options['timeout']) or options['timeout'] <= 0
                or not 1 <= options['batch_size'] <= 100):
            raise CommandError('Use finite nonnegative delay, positive timeout and batch size 1–100')
        vocabulary, using = options['source_vocabulary'], options['database']
        if not SourceCodeConceptMapping.objects.using(using).filter(source_vocabulary_id=vocabulary).exists():
            raise CommandError(f'No source mappings for vocabulary {vocabulary!r}; use the exact stored vocabulary ID')
        snapshots = destination_snapshots(vocabulary, using)
        log = self.stderr if options['report'] == '-' else self.stdout
        log.write(f'{vocabulary}: {len(snapshots)} distinct standard destinations; '
                  + ('apply (domain only)' if options['apply'] else 'dry run'))
        browser = AthenaDomainBrowser(interval=options['delay'], timeout=options['timeout'],
            executable=options['browser_executable'], storage_state=options['browser_storage_state'])
        totals = Counter()
        with ExitStack() as stack:
            writer = None
            if options['report']:
                stream = self.stdout if options['report'] == '-' else stack.enter_context(
                    open(options['report'], 'w', encoding='utf-8', newline=''))
                writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
                writer.writeheader()
            for start in range(0, len(snapshots), options['batch_size']):
                batch = snapshots[start:start + options['batch_size']]
                ids = [row['concept_id'] for row in batch if external(row)]
                log.write(f'Checking destinations {start + 1}–{start + len(batch)} of {len(snapshots)}')
                try:
                    evidence = browser.lookup_many(ids) if ids else {}
                except Exception as exc:
                    # An unavailable browser must not be retried for every remaining batch.
                    message = 'Earlier batches may be applied.' if options['apply'] else 'No changes were made.'
                    raise CommandError(f'Athena browser failed: {exc}. {message}') from exc
                for snapshot in batch:
                    receipt = reconcile_domain(snapshot, evidence.get(snapshot['concept_id'], {}),
                                               using=using, apply=options['apply'])
                    receipt.update(source_vocabulary=vocabulary, checked_at=datetime.now(timezone.utc).isoformat())
                    totals[receipt['outcome']] += 1
                    if writer:
                        writer.writerow(receipt)
                    else:
                        stale = f' stale_mappings={receipt["stale_mappings"]}' if receipt['stale_mappings'] else ''
                        log.write(f'{receipt["concept_id"]}: {receipt["local_domain"]} → '
                                  f'{receipt["athena_domain"] or "?"}; {receipt["outcome"]} {receipt["reason"]}{stale}')
                if writer:
                    stream.flush()
        log.write(f'{vocabulary}: checked={sum(totals.values())}; ' + ', '.join(f'{k}={v}' for k, v in sorted(totals.items())))
        unresolved = sum(totals[key] for key in UNRESOLVED)
        if unresolved:
            raise CommandError(f'{unresolved} destinations remain unresolved; see the report. '
                               + ('Successful domain corrections were applied.' if options['apply'] else 'No changes were made.'))
