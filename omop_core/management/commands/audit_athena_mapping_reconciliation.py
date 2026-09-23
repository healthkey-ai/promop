"""Explain why approved ICD-10 SCCM mappings did or did not become Athena mappings."""
import csv
from collections import Counter
from contextlib import ExitStack

from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
from django.db.models import Count

from omop_core.data_migrations.athena_icd10_stcm_reconcile import ICD10_VOCABS
from omop_core.models import SourceCodeConceptMapping as Mapping, SourceToConceptMap as STCM
from omop_core.services.stcm_mapping_audit import audit_batch

REPORT_FIELDS = (
    'mapping_id', 'source_vocabulary', 'source_code', 'lookup_vocabulary',
    'current_provenance', 'current_target_id', 'exact_vocabulary_code_matches',
    'lookup_matches', 'current_stcm_matches', 'valid_standard_target_ids',
    'proposed_target_id', 'proposed_vocabulary', 'proposed_domain', 'outcome',
)
MIGRATION = '0256_reconcile_icd10_mapped_against_stcm'


class Command(BaseCommand):
    help = ('Audit migration 0256: match approved non-Athena SCCM mappings against STCM. '
            'Default is read-only; --apply changes mapping definitions for future imports only.')

    def add_arguments(self, parser):
        parser.add_argument('vocabulary', nargs='?', choices=ICD10_VOCABS,
                            help='Exact stored source vocabulary; default: both ICD10 and ICD10CM')
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument('--apply', action='store_true')
        mode.add_argument('--dry-run', action='store_true', help='Read-only (default)')
        parser.add_argument('--database', default='default', choices=list(connections))
        parser.add_argument('--report', help='CSV path, or - for CSV on stdout')

    def handle(self, *args, **options):
        using = options['database']
        vocabularies = [options['vocabulary']] if options['vocabulary'] else ICD10_VOCABS
        log = self.stderr if options['report'] == '-' else self.stdout
        # Open the report before any possible apply, so a bad path cannot hide changes.
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
            log.write('Apply (SCCM only)' if options['apply'] else 'Dry run: no database writes')
            recorder = MigrationRecorder(connections[using])
            applied = (recorder.migration_qs.filter(app='omop_core', name=MIGRATION)
                       .values_list('applied', flat=True).first()) if recorder.has_table() else None
            log.write(f'Migration {MIGRATION}: {applied.isoformat() if applied else "not recorded as applied"}')
            log.write('STCM inventory (all stored ICD-10 vocabulary labels):')
            for group in STCM.objects.using(using).filter(
                source_vocabulary_id__icontains='ICD',
            ).values('source_vocabulary_id').annotate(rows=Count('pk')).order_by('source_vocabulary_id'):
                log.write(f'  {group["source_vocabulary_id"]}: {group["rows"]}')
            for vocabulary in ICD10_VOCABS:
                count = STCM.objects.using(using).filter(source_vocabulary_id=vocabulary).count()
                log.write(f'STCM {vocabulary}: {count} rows')
            query = Mapping.objects.using(using).filter(source_vocabulary_id__in=vocabularies)
            log.write('SCCM inventory:')
            for group in query.values('source_vocabulary_id', 'status', 'origin_system').annotate(
                rows=Count('pk'),
            ).order_by('source_vocabulary_id', 'status', 'origin_system'):
                log.write(f'  {group["source_vocabulary_id"]} status={group["status"]} '
                          f'provenance={group["origin_system"]!r}: {group["rows"]}')
            # Materialize initial snapshots. Pagination by offset while applying would
            # skip rows as successfully reconciled mappings leave this queryset.
            originals = list(query.filter(status='approved').exclude(origin_system='athena').order_by('pk'))
            log.write(f'Approved non-Athena mappings: {len(originals)}')
            totals = Counter()
            exact_matches = lookup_matches = 0
            for start in range(0, len(originals), 250):
                receipts = audit_batch(originals[start:start + 250], using=using, apply=options['apply'])
                for receipt in receipts:
                    totals[receipt['outcome']] += 1
                    exact_matches += bool(receipt['exact_vocabulary_code_matches'])
                    lookup_matches += bool(receipt['lookup_matches'])
                    if writer:
                        writer.writerow(receipt)
                if writer:
                    stream.flush()
                log.write(f'Checked {min(start + 250, len(originals))}/{len(originals)}')
            log.write(f'SCCM rows with a direct vocabulary/code STCM match: {exact_matches}')
            log.write(f'SCCM rows with an STCM match using migration lookup rules: {lookup_matches}')
            log.write('HT-One ICD10 uses ICD10CM evidence; other rows use their exact vocabulary. '
                      'Codes are trimmed and case-insensitive, as in migration 0256.')
            log.write('Outcomes: ' + (', '.join(f'{key}={value}' for key, value in sorted(totals.items())) or 'none'))
            if not options['apply']:
                log.write('No changes were made. Review the report before a separate --apply run.')
