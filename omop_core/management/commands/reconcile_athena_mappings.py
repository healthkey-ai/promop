"""Reattribute approved ICD-10 mappings using the original Athena export."""
import csv
import hashlib
import tempfile
from collections import Counter
from contextlib import ExitStack, redirect_stdout
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connections

from omop_core.management.commands.load_athena_vocabularies import (
    DEFAULT_GDRIVE_URL, _VocabularyArchive, _download_gdrive_vocabulary, _open_tsv,
)
from omop_core.models import SourceCodeConceptMapping as Mapping
from omop_core.services.athena_destinations import ArchiveLookup, Evidence, LookupFailure
from omop_core.services.athena_mapping_reconciliation import REPORT_FIELDS, reconcile_batch
from omop_core.services.stcm_mapping_audit import mapping_key


class Command(BaseCommand):
    help = ('Reconcile approved non-Athena SCCM mappings from original Athena Maps to evidence. '
            'Default: read-only, using the configured default Google Drive export.')

    def add_arguments(self, parser):
        parser.add_argument('vocabulary', nargs='?', choices=('ICD10', 'ICD10CM'),
                            help='Exact stored vocabulary; default: both ICD10 and ICD10CM')
        sources = parser.add_mutually_exclusive_group()
        sources.add_argument('--path', help='Directory containing original Athena TSV files')
        sources.add_argument('--archive', help='Original Athena ZIP file')
        sources.add_argument('--gdrive', nargs='?', const=DEFAULT_GDRIVE_URL,
                             help='Athena Drive folder or ZIP URL; default: the configured default folder')
        modes = parser.add_mutually_exclusive_group()
        modes.add_argument('--apply', action='store_true', help='Update SCCM only, for future imports')
        modes.add_argument('--dry-run', action='store_true', help='No database writes (default)')
        parser.add_argument('--database', default='default', choices=list(connections))
        parser.add_argument('--report', help='CSV file, or - for CSV on stdout')

    def handle(self, *args, **options):
        using = options['database']
        log = self.stderr if options['report'] == '-' else self.stdout
        vocabularies = [options['vocabulary']] if options['vocabulary'] else ['ICD10', 'ICD10CM']
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
            log.write('Apply: SCCM definitions for future imports only' if options['apply'] else 'Dry run: no database writes')
            originals = list(Mapping.objects.using(using).filter(
                source_vocabulary_id__in=vocabularies, status='approved',
            ).exclude(origin_system='athena').order_by('pk'))
            log.write(f'Approved non-Athena mappings: {len(originals)}')
            if not originals:
                log.write('Nothing to reconcile; no export download or changes needed.')
                return
            # All archive reads and validation finish before any database mutations.
            # Never substitute the local relationship table: approvals can write it.
            try:
                provider = self._provider(options, originals, stack, log)
            except (OSError, ValueError, KeyError, LookupFailure, csv.Error) as exc:
                raise CommandError(f'Cannot read complete Athena export: {exc}. No changes were made.') from exc
            totals = Counter()
            for start in range(0, len(originals), 250):
                receipts = reconcile_batch(originals[start:start + 250], provider,
                                           using=using, apply=options['apply'])
                for receipt in receipts:
                    totals[receipt['outcome']] += 1
                    if writer:
                        writer.writerow(receipt)
                if writer:
                    stream.flush()
                log.write(f'Checked {min(start + 250, len(originals))}/{len(originals)}')
            log.write('Outcomes: ' + ', '.join(f'{key}={value}' for key, value in sorted(totals.items())))
            if not options['apply']:
                log.write('No changes were made. Review the CSV before a separate --apply run.')

    def _provider(self, options, originals, stack, log):
        path, archive = options['path'], options['archive']
        reference = ''
        if not path and not archive:
            url = options['gdrive'] or DEFAULT_GDRIVE_URL
            download_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='promop-athena-reconcile-')))
            # gdown prints folder member names to stdout even though its
            # progress bars use stderr. Keep --report - a valid CSV stream.
            with redirect_stdout(self.stderr):
                archive = _download_gdrive_vocabulary(url, download_dir, log.write)
            reference = url + '; '
        if archive:
            archive = Path(archive).expanduser()
            with archive.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            reference += f'Athena ZIP sha256={digest}'
            bundle = _VocabularyArchive(archive, log.write)
            open_file = bundle.open
        else:
            path = Path(path).expanduser()
            digests = []
            for name in ('CONCEPT.csv', 'CONCEPT_RELATIONSHIP.csv'):
                with (path / name).open('rb') as stream:
                    digests.append(f'{name} sha256={hashlib.file_digest(stream, "sha256").hexdigest()}')
            reference = '; '.join(digests)

            def open_file(name):
                return _open_tsv(path, name)

        # Optional reference files are not needed: this command requires the
        # destination concepts already loaded and never creates vocabulary rows.
        def open_optional(name):
            try:
                return open_file(name)
            except CommandError as exc:
                raise FileNotFoundError(str(exc)) from exc

        keys = {(vocab, code.casefold()) for vocab, code in map(mapping_key, originals)}
        log.write('Reading complete CONCEPT.csv and CONCEPT_RELATIONSHIP.csv export evidence...')
        provider = ArchiveLookup(open_optional, keys, date.today(), reference)
        for key in keys:
            provider.results.setdefault(key, Evidence(reference=reference))
        log.write(f'Export read complete. Evidence fingerprint: {reference}')
        return provider
