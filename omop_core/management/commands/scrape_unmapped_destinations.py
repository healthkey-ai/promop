"""Recover Athena destinations: local export (or Drive) -> API -> web."""
import csv
import math
import tempfile
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import date
from pathlib import Path

from django.core.management.base import CommandError
from django.db import DatabaseError
import requests

from omop_core.management.embedding_command import EmbeddingLoadCommand
from omop_core.management.commands.load_athena_vocabularies import (
    _VocabularyArchive, _download_gdrive_vocabulary, _open_tsv,
)
from omop_core.services.athena_destinations import ArchiveLookup, AthenaAPI, AthenaBrowser, LookupFailure
from omop_core.services.destination_recovery import apply_evidence_batch, recoverable_mappings
from omop_core.models import SourceCodeConceptMapping

DEFAULT_LOCAL_PATH = '~/Downloads/vocabulary_download_v5'
DEFAULT_GDRIVE_URL = 'https://drive.google.com/drive/u/1/folders/1HoRWGepqcH3pMKK03KNb1oWpaVs0Avl7'
REPORT_FIELDS = ['mapping_id', 'vocabulary', 'lookup_vocabulary', 'code', 'tier', 'source_concept_id',
                 'target_concept_ids', 'outcome', 'coverage', 'reason', 'reference', 'attempts', 'errors']
TIERS = ('local directory', 'gdrive', 'API', 'web')


def archive_lookup(path, keys, today, reference, log, *, zipped=False):
    bundle = _VocabularyArchive(path, log) if zipped else None

    def open_file(name):
        try:
            return bundle.open(name) if bundle else _open_tsv(path, name)
        except CommandError as exc:
            raise FileNotFoundError(str(exc)) from exc

    return ArchiveLookup(open_file, keys, today, reference)


class Command(EmbeddingLoadCommand):
    help = 'Approve unambiguous Athena destinations for unmapped queue codes, preserving curated mappings.'

    def add_arguments(self, parser):
        parser.add_argument('--vocabulary', action='append', help='Exact source vocabulary; repeatable. Default: ICD10 and ICD10CM.')
        parser.add_argument('--lookup-vocabulary', action='append', default=[], metavar='SOURCE=ATHENA',
                            help='Explicit source-label correction, e.g. ICD10=ICD10CM for HT-One; repeatable')
        sources = parser.add_mutually_exclusive_group()
        sources.add_argument('--path', default=DEFAULT_LOCAL_PATH, help='First source: Athena TSV directory (default: %(default)s)')
        sources.add_argument('--archive', help='Use a local Athena ZIP instead of the first-source directory')
        parser.add_argument('--gdrive', default=DEFAULT_GDRIVE_URL, help='Export source when the local export is unavailable')
        parser.add_argument('--skip-download', action='store_true', help='Skip Google Drive; retain local, API and web lookups')
        parser.add_argument('--offline', action='store_true', help='Use only the local directory/archive, without network requests')
        parser.add_argument('--no-web', action='store_true', help='Disable the optional Playwright web fallback')
        parser.add_argument('--browser-storage-state', help='Playwright storage-state file for an authorized Athena session')
        parser.add_argument('--browser-executable', help='Optional Chrome/Chromium executable path')
        parser.add_argument('--limit', type=int, default=0, help='Maximum eligible queue rows (0: all)')
        parser.add_argument('--delay', type=float, default=1.0, help='Minimum seconds between requests/navigations')
        parser.add_argument('--timeout', type=float, default=30, help='Timeout per HTTP request/browser response')
        parser.add_argument('--retries', type=int, default=2, help='Retries for HTTP 429 and 5xx responses')
        parser.add_argument('--dry-run', action='store_true', help='Discover and validate without any database writes')
        parser.add_argument('--report', help='Per-code CSV path; use - for CSV on stdout')

    def handle(self, **options):
        if (not math.isfinite(options['delay']) or not math.isfinite(options['timeout'])
                or options['limit'] < 0 or options['delay'] < 0 or options['timeout'] <= 0
                or not 0 <= options['retries'] <= 5):
            raise CommandError('Use nonnegative limit/delay, positive timeout, and 0–5 retries.')
        today = date.today()
        lookup_vocabularies = {}
        for alias in options['lookup_vocabulary']:
            parts = alias.split('=')
            if len(parts) != 2 or any(not part.strip() or len(part.strip()) > 20 for part in parts):
                raise CommandError('--lookup-vocabulary must be SOURCE=ATHENA with nonempty vocabulary IDs.')
            source, destination = (part.strip() for part in parts)
            if source in lookup_vocabularies and lookup_vocabularies[source] != destination:
                raise CommandError(f'Conflicting lookup vocabularies for {source}')
            lookup_vocabularies[source] = destination
        vocabularies = options['vocabulary'] or ['ICD10', 'ICD10CM']
        # Audit every unapproved queue code, including existing proposals, but
        # keep the stricter write eligibility separate from coverage counts.
        query = SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id__in=vocabularies, status__in=['proposed', 'none', ''],
        ).exclude(source_code='').order_by('pk')
        if options['limit']:
            query = query[:options['limit']]
        snapshots = list(query.values('id', 'source_vocabulary_id', 'source_code', 'updated_at'))
        writable = set(recoverable_mappings().filter(pk__in=[r['id'] for r in snapshots]).values_list('pk', flat=True))
        summary = self.stderr if options['report'] == '-' else self.stdout
        summary.write(f'Unapproved queue rows to audit: {len(snapshots)}; eligible for recovery: {len(writable)}')
        totals = defaultdict(Counter)
        coverage = defaultdict(dict)
        api_disabled = False
        with ExitStack() as stack:
            writer = None
            if options['report']:
                stream = self.stdout if options['report'] == '-' else stack.enter_context(
                    open(options['report'], 'w', encoding='utf-8', newline=''))
                writer = csv.DictWriter(stream, fieldnames=REPORT_FIELDS)
                writer.writeheader()
            # A readable local export replaces Drive for the entire batch,
            # including codes absent from that export.
            keys = {(lookup_vocabularies.get(r['source_vocabulary_id'], r['source_vocabulary_id']),
                     r['source_code'].strip().casefold()) for r in snapshots}
            providers, metadata = [], {}
            export_available = False
            if keys:
                local = Path(options['archive'] or options['path']).expanduser()
                try:
                    summary.write(f'Scanning local Athena export: {local}')
                    provider = archive_lookup(local, keys, today, str(local), summary.write,
                                              zipped=bool(options['archive']))
                    providers.append(('local directory', provider, ''))
                    export_available = True
                    metadata.update({field: dict(rows) for field, rows in provider.metadata.items()})
                except (CommandError, LookupFailure, OSError, ValueError, KeyError) as exc:
                    providers.append(('local directory', None, str(exc)))
                    summary.write(f'Local export unavailable: {exc}')
                    if options['offline']:
                        raise CommandError(str(exc)) from exc
            if keys and not export_available and not options['offline'] and not options['skip_download']:
                try:
                    temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix='promop-destination-recovery-'))
                    archive = _download_gdrive_vocabulary(options['gdrive'], Path(temporary), summary.write)
                    provider = archive_lookup(archive, keys, today, options['gdrive'], summary.write, zipped=True)
                    providers.append(('gdrive', provider, ''))
                    export_available = True
                    for field, rows in provider.metadata.items():
                        metadata.setdefault(field, {}).update(rows)
                except (CommandError, LookupFailure, OSError, ValueError, KeyError) as exc:
                    providers.append(('gdrive', None, str(exc)))
                    summary.write(f'Google Drive export unavailable: {exc}')
            if snapshots and not options['offline']:
                api = AthenaAPI(interval=options['delay'], timeout=options['timeout'], retries=options['retries'])
                stack.callback(api.close)
                providers.append(('API', api, ''))
                if not options['no_web']:
                    providers.append(('web', AthenaBrowser(interval=options['delay'], timeout=options['timeout'],
                        storage_state=options['browser_storage_state'], executable=options['browser_executable']), ''))
            pending = []

            def finish_batch():
                items = [item for _, item in pending if item]
                try:
                    applied = apply_evidence_batch(items, today, dry_run=options['dry_run'])
                except (LookupFailure, DatabaseError, ValueError) as exc:
                    applied = {item['snapshot']['id']: ('unmapped', str(exc)) for item in items}
                for record, item in pending:
                    if item:
                        record['outcome'], failure = applied[record['mapping_id']]
                        if failure:
                            record['reason'] = failure
                    totals[record['vocabulary']][record['outcome']] += 1
                    if writer:
                        writer.writerow(record)
                    else:
                        summary.write(f'{record["vocabulary"]}:{record["code"]}: '
                                      f'{record["tier"] or "no mapping"}, {record["outcome"]} {record["reason"]}'
                                      + (f' ({record["errors"]})' if record['errors'] else ''))
                if writer:
                    stream.flush()
                pending.clear()

            for snapshot in snapshots:
                vocabulary, code = snapshot['source_vocabulary_id'], snapshot['source_code']
                lookup_vocabulary = lookup_vocabularies.get(vocabulary, vocabulary)
                totals[vocabulary]['searched'] += 1
                report = dict.fromkeys(REPORT_FIELDS, '')
                report.update(mapping_id=snapshot['id'], vocabulary=vocabulary, lookup_vocabulary=lookup_vocabulary, code=code,
                              outcome='unmapped', reason='not_found')
                errors, attempts = [], []
                apply_item = None
                web_complete, api_failed = False, False
                for tier, provider, error in providers:
                    if tier == 'API' and api_disabled:
                        continue
                    attempts.append(tier)
                    if error:
                        errors.append(f'{tier}: {error}')
                        continue
                    try:
                        evidence = provider.lookup(lookup_vocabulary, code, today)
                    except (LookupFailure, requests.RequestException, ValueError, KeyError, TypeError, OSError) as exc:
                        if (tier == 'API' and isinstance(exc, requests.HTTPError)
                                and exc.response is not None and exc.response.status_code == 403):
                            api_disabled = True
                            attempts.pop()
                            summary.write('Athena API returned HTTP 403; disabling API lookups for this run.')
                            continue
                        if tier == 'API':
                            api_failed = True
                        errors.append(f'{tier}: {type(exc).__name__}: {exc}')
                        continue
                    except Exception as exc:
                        # Playwright is optional, so its exception types cannot be
                        # imported until the web tier is actually needed.
                        if tier != 'web':
                            raise
                        errors.append(f'web: {type(exc).__name__}: {exc}')
                        continue
                    if evidence.reason == 'not_found':
                        if tier == 'web':
                            web_complete = True
                        continue
                    report.update(tier=tier, source_concept_id=(evidence.source or {}).get('concept_id', ''),
                        target_concept_ids=';'.join(str(t['concept_id']) for t in evidence.targets),
                        reason=evidence.reason, reference=evidence.reference)
                    if evidence.reason in ('found', 'ambiguous_targets'):
                        totals[vocabulary]['found_' + tier] += 1
                        reference_metadata = provider.metadata if isinstance(provider, ArchiveLookup) else metadata
                        if snapshot['id'] in writable:
                            apply_item = dict(snapshot=snapshot, evidence=evidence, tier=tier,
                                              metadata=reference_metadata, lookup_vocabulary=lookup_vocabulary)
                        else:
                            report['outcome'] = 'preserved_existing'
                        report['reason'] = 'multiple_destinations' if len(evidence.targets) > 1 else ''
                    # Conflicting sources or local concepts are not absence.
                    # Never replace evidence by trying a lower-priority provider.
                    break
                report['errors'] = ' | '.join(errors)
                report['attempts'] = ' -> '.join(attempts)
                if report['target_concept_ids']:
                    report['coverage'] = 'multiple_destinations' if ';' in report['target_concept_ids'] else 'found'
                elif report['reason'] == 'ambiguous_source':
                    report['coverage'] = 'ambiguous'
                elif export_available and web_complete and not api_failed:
                    report['coverage'] = 'not_found_any_method'
                else:
                    report['coverage'] = 'incomplete_lookup'
                    report['reason'] = 'lookup_errors' if errors else 'methods_disabled'
                # Count unique vocabulary/code pairs, not duplicate queue rows.
                key = code.strip().casefold()
                priority = {'not_found_any_method': 0, 'incomplete_lookup': 1, 'found': 2,
                            'multiple_destinations': 3, 'ambiguous': 4}
                previous = coverage[vocabulary].get(key, 'not_found_any_method')
                coverage[vocabulary][key] = max(previous, report['coverage'], key=priority.get)
                pending.append((report, apply_item))
                if len(pending) >= 100:
                    finish_batch()
            finish_batch()
        for vocabulary, counts in sorted(totals.items()):
            tiers = [tier for tier in TIERS if tier != 'API' or not api_disabled or counts['found_API']]
            fields = ['searched', *('found_' + tier for tier in tiers), 'loaded', 'loaded_multiple', 'would_load', 'would_load_multiple',
                      'unmapped', 'preserved_existing', 'skipped_changed', 'skipped_existing_mapping']
            summary.write(f'{vocabulary}: ' + ', '.join(f'{key}={counts[key]}' for key in fields))
            code_counts = Counter(coverage[vocabulary].values())
            summary.write(f'{vocabulary} unique codes: checked={len(coverage[vocabulary])}, '
                          + ', '.join(f'{key}={code_counts[key]}' for key in
                                      ('found', 'multiple_destinations', 'ambiguous', 'not_found_any_method', 'incomplete_lookup')))
        overall = Counter(value for codes in coverage.values() for value in codes.values())
        summary.write(f'TOTAL unique vocabulary/code pairs: checked={sum(overall.values())}, '
                      + ', '.join(f'{key}={overall[key]}' for key in
                                  ('found', 'multiple_destinations', 'ambiguous', 'not_found_any_method', 'incomplete_lookup')))
