import csv
import sys
import time
from datetime import date
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.models import Count

import logging

from omop_core.models import (
    Vocabulary, Domain, ConceptClass, Concept,
    Relationship, ConceptRelationship, ConceptAncestor, CdmSource,
    VocabularyVersionHistory, record_vocabulary_version_history,
    VocabularyRelease,
)

logger = logging.getLogger(__name__)

VOCAB_SCOPE = frozenset({
    'HemOnc', 'RxNorm', 'RxNorm Extension', 'ATC', 'LOINC', 'UCUM',
    'Visit', 'Type Concept',
    # Clinical-record vocabularies (FHIR sync B3): SNOMED covers conditions,
    # procedures, and allergies; ICD10CM covers EHR-sourced diagnoses. CVX
    # (immunizations) is mapped in the ingest but isn't in the current Athena
    # export, so it loads once a CVX-inclusive bundle is fetched.
    'SNOMED', 'ICD10CM', 'CVX',
})
# These vocabularies underpin the clinical concepts PROMOP presents and maps.
# Do not include CVX here: it is deliberately absent from the current Athena
# bundle, even though the importer supports it when a CVX-inclusive bundle is
# used.
REQUIRED_CLINICAL_VOCABULARIES = frozenset({'LOINC', 'RxNorm', 'SNOMED', 'ICD10CM'})
RXNORM_CLASS_SCOPE = frozenset({'Ingredient', 'Clinical Drug', 'Branded Drug', 'Clinical Drug Comp'})
# A --replace reload deletes the entire vocabulary before applying this filter.
# Keep every LOINC domain already required by our deployed vocabulary; the
# preflight below turns any future scope drift into a safe, actionable failure.
LOINC_DOMAIN_SCOPE = frozenset({
    'Measurement', 'Observation', 'Meas Value', 'Procedure', 'Note',
})
BATCH = 100_000
PROGRESS_EVERY = 500_000

# Defaults for the single self-describing cdm_source row. Kept in sync with
# migration 0112_seed_cdm_source; the command re-seeds this row because a
# --replace TRUNCATE ... CASCADE wipes cdm_source (it FKs to concept).
_CDM_SOURCE_DEFAULTS = {
    'cdm_source_name': 'PRomop — Decision-Ready Longitudinal Patient Record',
    'cdm_holder': 'HealthKey, Inc.',
    'source_description': (
        'PRomop longitudinal patient record on the OMOP CDM 5.4 clinical '
        'tables with OHDSI oncology extensions and a derived PatientRecord projection.'
    ),
    'source_documentation_reference': 'https://github.com/healthkey-ai',
    'cdm_etl_reference': 'https://github.com/healthkey-ai',
    'cdm_release_date': date(2026, 1, 1),
    'cdm_version': '5.4',
}


def _parse_date(s):
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return date.fromisoformat(s)


def _open_tsv(base, filename):
    path = Path(base) / filename
    if not path.exists():
        raise CommandError(f'Required file not found: {path}')
    return open(path, encoding='utf-8', newline='')


def _download_gcs_blob(bucket, filename, log):
    blob = bucket.blob(filename)
    if not blob.exists():
        raise CommandError(f'Required blob not found: gs://{bucket.name}/{filename}')
    dest = Path('/tmp/vocab') / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    size_mb = (blob.size or 0) / 1048576
    log(f'  Downloading {filename} ({size_mb:.0f}MB)...')
    t = time.monotonic()
    blob.download_to_filename(str(dest))
    elapsed = time.monotonic() - t
    log(f'  Downloaded {filename} in {elapsed:.0f}s.')
    return open(dest, encoding='utf-8', newline='')


def _header_index(header_row):
    """Build column-name → index map from a TSV header row."""
    return {col: i for i, col in enumerate(header_row)}


def _concept_in_scope(vid, concept_code, concept_class_id, domain_id):
    if vid not in VOCAB_SCOPE:
        return False
    if vid == 'ATC':
        return concept_code.startswith('L')
    if vid in ('RxNorm', 'RxNorm Extension'):
        return concept_class_id in RXNORM_CLASS_SCOPE
    if vid == 'LOINC':
        return domain_id in LOINC_DOMAIN_SCOPE
    return True


def _copy_rows(table, columns, rows, log, direct=False):
    """COPY rows into table. direct=True skips temp table (use after TRUNCATE)."""
    if not rows:
        return
    connection.ensure_connection()
    cols = ', '.join(columns)
    with connection.connection.cursor() as cur:
        if direct:
            with cur.copy(f'COPY {table} ({cols}) FROM STDIN') as copy:
                for row in rows:
                    copy.write_row(row)
        else:
            tmp = f'_tmp_{table}'
            cur.execute(f'DROP TABLE IF EXISTS {tmp}')
            cur.execute(
                f'CREATE TEMP TABLE {tmp} AS SELECT {cols} FROM {table} WHERE false'
            )
            with cur.copy(f'COPY {tmp} ({cols}) FROM STDIN') as copy:
                for row in rows:
                    copy.write_row(row)
            cur.execute(f'INSERT INTO {table} ({cols}) SELECT {cols} FROM {tmp} ON CONFLICT DO NOTHING')
            cur.execute(f'DROP TABLE {tmp}')


class Command(BaseCommand):
    help = 'Load OHDSI Athena vocabulary TSV files into OMOP vocabulary tables'

    def add_arguments(self, parser):
        parser.add_argument('--path',
                            help='Directory containing Athena TSV files')
        parser.add_argument('--bucket',
                            help='GCS bucket name to stream files from (alternative to --path)')
        parser.add_argument('--replace', action='store_true',
                            help='Clear vocabulary rows before loading')
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Count rows without writing to DB')
        parser.add_argument('--skip-clinical-vocabulary-verification', action='store_true',
                            help='Do not verify that required clinical vocabularies loaded')

    def handle(self, *args, **options):
        base = options['path']
        bucket_name = options['bucket']
        replace = options['replace']
        dry_run = options['dry_run']
        skip_clinical_vocabulary_verification = options['skip_clinical_vocabulary_verification']

        if not base and not bucket_name:
            raise CommandError('Provide either --path or --bucket')

        self._gcs_bucket = None
        if bucket_name:
            from google.cloud import storage as gcs
            self._gcs_bucket = gcs.Client().bucket(bucket_name)
            self._log(f'Loading from gs://{bucket_name}/ (download-one-process-delete)')

        t0 = time.monotonic()
        self._build_start = time.time()  # wall-clock for VocabularyRelease

        self._base = base

        self._direct = replace and not dry_run

        if replace:
            logger.warning(
                '--replace uses TRUNCATE CASCADE and will destroy clinical '
                'data in tables that FK to concept (drug_exposure, measurement, '
                'condition_occurrence, etc.). The default upsert path (no flag) '
                'is safe for databases with clinical data.'
            )
            self._validate_replace_loinc_scope()

        if self._direct:
            self._clear()

        counts = {
            'relationship':         self._load_relationships(dry_run),
            'vocabulary':           self._load_vocabularies(dry_run),
            'domain':               self._load_domains(dry_run),
            'concept_class':        self._load_concept_classes(dry_run),
            'concept':              self._load_concepts(dry_run),
            'concept_relationship': self._load_concept_relationships(dry_run),
            'concept_ancestor':     self._load_concept_ancestors(dry_run),
            'concept_synonym':      self._load_concept_synonym(dry_run),
            'drug_strength':        self._load_drug_strength(dry_run),
            'source_to_concept_map': self._load_source_to_concept_map(dry_run),
        }
        if not dry_run:
            self._seed_concept_zero()
            self._sync_cdm_source_metadata()
            if not skip_clinical_vocabulary_verification:
                self._verify_required_clinical_vocabularies()
            self._record_version_history(replace)
            self._publish_release(counts)
        elapsed = time.monotonic() - t0
        verb = 'would load' if dry_run else 'loaded'
        total = sum(counts.values())
        self._log('')
        self._log('=' * 60)
        self._log('  LOAD SUMMARY')
        self._log('=' * 60)
        for table, n in counts.items():
            self._log(f'  {table:.<30s} {n:>12,} rows {verb}')
        self._log(f'  {"TOTAL":.<30s} {total:>12,} rows')
        self._log('-' * 60)
        if hasattr(self, '_vocab_counts') and self._vocab_counts:
            self._log('  Concepts by vocabulary:')
            for vid, n in sorted(self._vocab_counts.items(), key=lambda x: -x[1]):
                self._log(f'    {vid:.<28s} {n:>12,}')
            self._log(f'  Scanned {self._concept_scanned:,} concept rows ({total and counts["concept"] * 100 // self._concept_scanned or 0}% in scope)')
        self._log(f'  Elapsed: {elapsed:.0f}s')
        self._log('=' * 60)

    def _log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

    def _verify_required_clinical_vocabularies(self):
        """Fail the load when a partial Athena bundle omits core clinical vocabularies."""
        counts = dict(
            Concept.objects.filter(vocabulary_id__in=REQUIRED_CLINICAL_VOCABULARIES)
            .values('vocabulary_id')
            .annotate(total=Count('concept_id'))
            .values_list('vocabulary_id', 'total')
        )
        missing = sorted(REQUIRED_CLINICAL_VOCABULARIES - counts.keys())
        if missing:
            raise CommandError(
                'Required clinical vocabularies are missing after the load: '
                f"{', '.join(missing)}. This database cannot reliably map clinical "
                'conditions, diagnoses, medications, and labs. Fetch an Athena bundle '
                'that includes the missing vocabularies and rerun this command without '
                '--replace; --replace truncates clinical data.'
            )
        self._log(
            '  verified required clinical vocabularies: ' +
            ', '.join(f'{vid} ({counts[vid]:,})' for vid in sorted(counts))
        )

    def _open(self, filename):
        if self._gcs_bucket:
            return _download_gcs_blob(self._gcs_bucket, filename, self._log)
        return _open_tsv(self._base, filename)

    def _cleanup(self, filename):
        if self._gcs_bucket:
            tmp = Path('/tmp/vocab') / filename
            if tmp.exists():
                tmp.unlink()
                self._log(f'  Cleaned up {filename}.')

    def _clear(self):
        self._log('Clearing existing vocabulary data (TRUNCATE)...')
        t = time.monotonic()
        with connection.cursor() as cur:
            cur.execute(
                'TRUNCATE concept_ancestor, concept_relationship, '
                'concept, concept_class, domain, relationship, vocabulary CASCADE'
            )
        self._log(f'  Truncated all vocab tables in {time.monotonic() - t:.0f}s')
        self._log('  NOTE: CASCADE also clears every table with a FK to concept — '
                  'including cdm_source, observation_period, and clinical event tables. '
                  'cdm_source is re-seeded after load; re-run populate_observation_period '
                  'to re-derive observation periods.')

    def _validate_replace_loinc_scope(self):
        """Abort before TRUNCATE if loaded LOINC data falls outside the filter.

        `--replace` first clears ``concept`` and then reloads only the configured
        LOINC domains. Without this check, adding a new LOINC domain to a live
        database can make a later ordinary reload silently delete its concepts.
        """
        excluded = Concept.objects.filter(vocabulary_id='LOINC').exclude(
            domain_id__in=LOINC_DOMAIN_SCOPE,
        )
        count = excluded.count()
        if not count:
            return

        domains = list(
            excluded.order_by('domain_id').values_list('domain_id', flat=True).distinct()
        )
        raise CommandError(
            '--replace aborted before TRUNCATE: '
            f'{count:,} loaded LOINC concept(s) use domain(s) outside '
            f'LOINC_DOMAIN_SCOPE: {", ".join(domains)}. '
            'Add the required domain(s) to LOINC_DOMAIN_SCOPE, then rerun.'
        )

    def _seed_concept_zero(self):
        Vocabulary.objects.get_or_create(
            vocabulary_id='None',
            defaults={'vocabulary_name': 'None', 'vocabulary_concept_id': 0},
        )
        Domain.objects.get_or_create(
            domain_id='Metadata',
            defaults={'domain_name': 'Metadata', 'domain_concept_id': 0},
        )
        ConceptClass.objects.get_or_create(
            concept_class_id='Undefined',
            defaults={'concept_class_name': 'Undefined', 'concept_class_concept_id': 0},
        )
        _, created = Concept.objects.get_or_create(
            concept_id=0,
            defaults={
                'concept_name': 'No matching concept',
                'domain_id': 'Metadata',
                'vocabulary_id': 'None',
                'concept_class_id': 'Undefined',
                'concept_code': 'No matching concept',
                'valid_start_date': '1970-01-01',
                'valid_end_date': '2099-12-31',
            },
        )
        if created:
            self._log('Seeded concept_id=0 (No matching concept)')

    def _load_relationships(self, dry_run):
        self._log('Loading RELATIONSHIP.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        with self._open('RELATIONSHIP.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                try:
                    row = (
                        cols[idx['relationship_id']][:20],
                        cols[idx['relationship_name']][:255],
                        int(cols[idx['is_hierarchical']] or 0),
                        int(cols[idx['defines_ancestry']] or 0),
                        cols[idx['reverse_relationship_id']][:20],
                        int(cols[idx['relationship_concept_id']] or 0),
                    )
                except (ValueError, KeyError, IndexError) as exc:
                    self._log(f'Warning: skipping malformed relationship row: {exc}')
                    continue
                count += 1
                if not dry_run:
                    rows.append(row)
                    if len(rows) >= BATCH:
                        _copy_rows('relationship',
                                   ('relationship_id', 'relationship_name', 'is_hierarchical',
                                    'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('relationship',
                       ('relationship_id', 'relationship_name', 'is_hierarchical',
                        'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'),
                       rows, self._log, direct=self._direct)
        self._cleanup('RELATIONSHIP.csv')
        self._log(f'  RELATIONSHIP.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_vocabularies(self, dry_run):
        self._log('Loading VOCABULARY.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        self._cdm_vocab_version = None
        try:
            f = self._open('VOCABULARY.csv')
        except CommandError:
            self._log('  VOCABULARY.csv not found, skipping.')
            return 0
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                vid = cols[idx['vocabulary_id']]
                # 'None' is out of scope for concept loading but its row carries
                # the CDM release version in vocabulary_version — keep it so
                # cdm_source can self-describe.
                if vid not in VOCAB_SCOPE and vid != 'None':
                    continue
                count += 1
                version = (cols[idx.get('vocabulary_version', -1)] if 'vocabulary_version' in idx else '')[:255] or ''
                if vid == 'None' and version:
                    self._cdm_vocab_version = version
                if not dry_run:
                    rows.append((
                        vid[:20],
                        cols[idx['vocabulary_name']][:255],
                        (cols[idx.get('vocabulary_reference', -1)] if 'vocabulary_reference' in idx else '')[:255] or '',
                        version,
                        int(cols[idx['vocabulary_concept_id']] or 0) if 'vocabulary_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('vocabulary',
                       ('vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
                        'vocabulary_version', 'vocabulary_concept_id'),
                       rows, self._log, direct=self._direct)
        if not dry_run and self._cdm_vocab_version:
            # The 'None' row usually pre-exists (seeded by migration 0068 with no
            # version), so the COPY above conflicts on the PK and no-ops — update
            # it directly so the CDM release version actually lands.
            Vocabulary.objects.filter(vocabulary_id='None').update(
                vocabulary_version=self._cdm_vocab_version
            )
        self._cleanup('VOCABULARY.csv')
        self._log(f'  VOCABULARY.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_domains(self, dry_run):
        self._log('Loading DOMAIN.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        try:
            f = self._open('DOMAIN.csv')
        except CommandError:
            self._log('  DOMAIN.csv not found, skipping.')
            return 0
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                count += 1
                if not dry_run:
                    rows.append((
                        cols[idx['domain_id']][:20],
                        cols[idx['domain_name']][:255],
                        int(cols[idx['domain_concept_id']] or 0) if 'domain_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('domain',
                       ('domain_id', 'domain_name', 'domain_concept_id'),
                       rows, self._log, direct=self._direct)
        self._cleanup('DOMAIN.csv')
        self._log(f'  DOMAIN.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_classes(self, dry_run):
        self._log('Loading CONCEPT_CLASS.csv...')
        t = time.monotonic()
        count = 0
        rows = []
        try:
            f = self._open('CONCEPT_CLASS.csv')
        except CommandError:
            self._log('  CONCEPT_CLASS.csv not found, skipping.')
            return 0
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            for cols in reader:
                count += 1
                if not dry_run:
                    rows.append((
                        cols[idx['concept_class_id']][:20],
                        cols[idx['concept_class_name']][:255],
                        int(cols[idx['concept_class_concept_id']] or 0) if 'concept_class_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('concept_class',
                       ('concept_class_id', 'concept_class_name', 'concept_class_concept_id'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_CLASS.csv')
        self._log(f'  CONCEPT_CLASS.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concepts(self, dry_run):
        self._log('Loading CONCEPT.csv...')
        t = time.monotonic()
        count = 0
        scanned = 0
        vocab_counts = {}
        rows = []
        with self._open('CONCEPT.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_id = idx['concept_id']
            i_name = idx['concept_name']
            i_domain = idx['domain_id']
            i_vocab = idx['vocabulary_id']
            i_class = idx['concept_class_id']
            i_std = idx['standard_concept']
            i_code = idx['concept_code']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_invalid = idx['invalid_reason']
            for cols in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  concepts: scanned {scanned:,}, {count:,} in scope ({time.monotonic() - t:.0f}s)...')
                vid = cols[i_vocab]
                if not _concept_in_scope(vid, cols[i_code], cols[i_class], cols[i_domain]):
                    continue
                try:
                    concept_id = int(cols[i_id])
                    start = _parse_date(cols[i_start])
                    end = _parse_date(cols[i_end])
                except (ValueError, IndexError) as exc:
                    self._log(f'Warning: skipping malformed concept row: {exc}')
                    continue
                count += 1
                vocab_counts[vid] = vocab_counts.get(vid, 0) + 1
                if not dry_run:
                    std = cols[i_std][:1] if cols[i_std] else None
                    inv = cols[i_invalid][:1] if cols[i_invalid] else None
                    rows.append((
                        concept_id,
                        cols[i_name][:255],
                        cols[i_domain][:20],
                        vid[:20],
                        cols[i_class][:20],
                        std,
                        cols[i_code][:50],
                        start.isoformat(),
                        end.isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('concept',
                                   ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
                                    'concept_class_id', 'standard_concept', 'concept_code',
                                    'valid_start_date', 'valid_end_date', 'invalid_reason'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept',
                       ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
                        'concept_class_id', 'standard_concept', 'concept_code',
                        'valid_start_date', 'valid_end_date', 'invalid_reason'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT.csv')
        self._log(f'  concepts: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        self._vocab_counts = vocab_counts
        self._concept_scanned = scanned
        return count

    def _load_concept_relationships(self, dry_run):
        self._log('Loading CONCEPT_RELATIONSHIP.csv...')
        t = time.monotonic()
        loaded_ids = set(
            Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE)
                           .values_list('concept_id', flat=True)
        )
        self._log(f'  {len(loaded_ids):,} concept IDs in filter set')
        count = 0
        scanned = 0
        rows = []
        with self._open('CONCEPT_RELATIONSHIP.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_c1 = idx['concept_id_1']
            i_c2 = idx['concept_id_2']
            i_rel = idx['relationship_id']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_invalid = idx['invalid_reason']
            for cols in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  relationships: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    c1 = int(cols[i_c1])
                    c2 = int(cols[i_c2])
                except (ValueError, IndexError):
                    continue
                if c1 not in loaded_ids or c2 not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    inv = cols[i_invalid][:1] if cols[i_invalid] else None
                    rows.append((
                        c1, c2,
                        cols[i_rel][:20],
                        _parse_date(cols[i_start]).isoformat(),
                        _parse_date(cols[i_end]).isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('concept_relationship',
                                   ('concept_id_1', 'concept_id_2', 'relationship_id',
                                    'valid_start_date', 'valid_end_date', 'invalid_reason'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept_relationship',
                       ('concept_id_1', 'concept_id_2', 'relationship_id',
                        'valid_start_date', 'valid_end_date', 'invalid_reason'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_RELATIONSHIP.csv')
        self._log(f'  relationships: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_ancestors(self, dry_run):
        self._log('Loading CONCEPT_ANCESTOR.csv...')
        t = time.monotonic()
        hemonc_ids = set(
            Concept.objects.filter(vocabulary_id='HemOnc')
                           .values_list('concept_id', flat=True)
        )
        self._log(f'  {len(hemonc_ids):,} HemOnc IDs in filter set')
        count = 0
        scanned = 0
        rows = []
        with self._open('CONCEPT_ANCESTOR.csv') as f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_anc = idx['ancestor_concept_id']
            i_desc = idx['descendant_concept_id']
            i_min = idx['min_levels_of_separation']
            i_max = idx['max_levels_of_separation']
            for cols in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  ancestors: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    anc = int(cols[i_anc])
                    desc = int(cols[i_desc])
                except (ValueError, IndexError):
                    continue
                if anc not in hemonc_ids or desc not in hemonc_ids:
                    continue
                count += 1
                if not dry_run:
                    try:
                        min_sep = int(cols[i_min])
                        max_sep = int(cols[i_max])
                    except (ValueError, IndexError):
                        continue
                    rows.append((anc, desc, min_sep, max_sep))
                    if len(rows) >= BATCH:
                        _copy_rows('concept_ancestor',
                                   ('ancestor_concept_id', 'descendant_concept_id',
                                    'min_levels_of_separation', 'max_levels_of_separation'),
                                   rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept_ancestor',
                       ('ancestor_concept_id', 'descendant_concept_id',
                        'min_levels_of_separation', 'max_levels_of_separation'),
                       rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_ANCESTOR.csv')
        self._log(f'  ancestors: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concept_synonym(self, dry_run):
        self._log('Loading CONCEPT_SYNONYM.csv...')
        t = time.monotonic()
        try:
            f = self._open('CONCEPT_SYNONYM.csv')
        except CommandError:
            self._log('  CONCEPT_SYNONYM.csv not found, skipping.')
            return 0
        loaded_ids = set(Concept.objects.values_list('concept_id', flat=True))
        count = scanned = 0
        rows = []
        cols_out = ('concept_id', 'concept_synonym_name', 'language_concept_id')
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_cid, i_name, i_lang = idx['concept_id'], idx['concept_synonym_name'], idx['language_concept_id']
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  synonyms: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    cid, lang = int(row[i_cid]), int(row[i_lang])
                except (ValueError, IndexError):
                    continue
                # both concept and language must be loaded to satisfy FK constraints
                if cid not in loaded_ids or lang not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    rows.append((cid, row[i_name][:1000], lang))
                    if len(rows) >= BATCH:
                        _copy_rows('concept_synonym', cols_out, rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('concept_synonym', cols_out, rows, self._log, direct=self._direct)
        self._cleanup('CONCEPT_SYNONYM.csv')
        self._log(f'  synonyms: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_drug_strength(self, dry_run):
        self._log('Loading DRUG_STRENGTH.csv...')
        t = time.monotonic()
        try:
            f = self._open('DRUG_STRENGTH.csv')
        except CommandError:
            self._log('  DRUG_STRENGTH.csv not found, skipping.')
            return 0
        loaded_ids = set(Concept.objects.values_list('concept_id', flat=True))
        count = scanned = 0
        rows = []
        cols_out = (
            'drug_concept_id', 'ingredient_concept_id', 'amount_value', 'amount_unit_concept_id',
            'numerator_value', 'numerator_unit_concept_id', 'denominator_value',
            'denominator_unit_concept_id', 'box_size', 'valid_start_date', 'valid_end_date',
            'invalid_reason',
        )

        def _fk(v):
            """Concept id if loaded, else None — keeps optional unit FKs valid."""
            try:
                iv = int(v)
            except (ValueError, TypeError):
                return None
            return iv if iv in loaded_ids else None

        def _f(v):
            v = (v or '').strip()
            return float(v) if v else None

        def _i(v):
            v = (v or '').strip()
            return int(v) if v else None

        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            gi = idx.get
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  drug_strength: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    drug = int(row[idx['drug_concept_id']])
                    ing = int(row[idx['ingredient_concept_id']])
                except (ValueError, IndexError):
                    continue
                # required FKs must be loaded
                if drug not in loaded_ids or ing not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    inv = row[idx['invalid_reason']][:1] if row[idx['invalid_reason']] else None
                    rows.append((
                        drug, ing,
                        _f(row[gi('amount_value')]), _fk(row[gi('amount_unit_concept_id')]),
                        _f(row[gi('numerator_value')]), _fk(row[gi('numerator_unit_concept_id')]),
                        _f(row[gi('denominator_value')]), _fk(row[gi('denominator_unit_concept_id')]),
                        _i(row[gi('box_size')]),
                        _parse_date(row[idx['valid_start_date']]).isoformat(),
                        _parse_date(row[idx['valid_end_date']]).isoformat(),
                        inv,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('drug_strength', cols_out, rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('drug_strength', cols_out, rows, self._log, direct=self._direct)
        self._cleanup('DRUG_STRENGTH.csv')
        self._log(f'  drug_strength: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _sync_cdm_source_metadata(self):
        """Ensure the cdm_source row exists and fill its vocabulary metadata.

        get_or_create (not just update) because --replace TRUNCATEs concept
        CASCADE, which also wipes cdm_source via its cdm_version_concept FK —
        the migration-0112 seed does not re-run after that.
        """
        row, created = CdmSource.objects.get_or_create(
            cdm_source_abbreviation='PRomop',
            defaults=_CDM_SOURCE_DEFAULTS,
        )
        if created:
            self._log('  cdm_source: re-seeded PRomop row (was missing — e.g. cleared by --replace)')
        vocab_version = (
            Vocabulary.objects.filter(vocabulary_id='None')
            .values_list('vocabulary_version', flat=True).first()
        )
        version_concept_id = 756265 if Concept.objects.filter(concept_id=756265).exists() else None
        fields = {}
        if vocab_version:
            fields['vocabulary_version'] = vocab_version[:20]
        if version_concept_id:
            fields['cdm_version_concept_id'] = version_concept_id
        if fields and CdmSource.objects.filter(pk=row.pk).update(**fields):
            self._log(f'  cdm_source: updated {fields}')

    def _record_version_history(self, replace):
        """Append an immutable version-history row per loaded vocabulary.

        Because --replace TRUNCATEs the vocabulary snapshot, the only durable
        record of which release was implemented when is this append-only table
        (promop#305, TI.4.2#01/#09). action='replaced' when --replace cleared a
        prior snapshot, else 'loaded'. cdm_release_date is taken from the
        self-describing cdm_source row.
        """
        action = (
            VocabularyVersionHistory.ACTION_REPLACED if replace
            else VocabularyVersionHistory.ACTION_LOADED
        )
        cdm_release_date = (
            CdmSource.objects.filter(cdm_source_abbreviation='PRomop')
            .values_list('cdm_release_date', flat=True).first()
        )
        recorded = 0
        for vid, version in (
            Vocabulary.objects.order_by('vocabulary_id')
            .values_list('vocabulary_id', 'vocabulary_version')
        ):
            record_vocabulary_version_history(
                vocabulary_id=vid,
                version=version,
                action=action,
                cdm_release_date=cdm_release_date,
            )
            recorded += 1
        self._log(f'  vocabulary_version_history: recorded {recorded} {action} row(s)')

    def _load_source_to_concept_map(self, dry_run):
        self._log('Loading SOURCE_TO_CONCEPT_MAP.csv...')
        t = time.monotonic()
        try:
            f = self._open('SOURCE_TO_CONCEPT_MAP.csv')
        except CommandError:
            self._log('  SOURCE_TO_CONCEPT_MAP.csv not found, skipping.')
            return 0
        loaded_ids = set(Concept.objects.values_list('concept_id', flat=True))
        count = scanned = 0
        rows = []
        cols_out = (
            'source_code', 'source_concept_id', 'source_vocabulary_id',
            'source_code_description', 'target_concept_id', 'target_vocabulary_id',
            'valid_start_date', 'valid_end_date', 'invalid_reason',
        )
        with f:
            reader = csv.reader(f, delimiter='\t')
            idx = _header_index(next(reader))
            i_src_code = idx['source_code']
            i_src_cid = idx['source_concept_id']
            i_src_vid = idx['source_vocabulary_id']
            i_src_desc = idx['source_code_description']
            i_tgt_cid = idx['target_concept_id']
            i_tgt_vid = idx['target_vocabulary_id']
            i_start = idx['valid_start_date']
            i_end = idx['valid_end_date']
            i_inv = idx['invalid_reason']
            for row in reader:
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    self._log(f'  stcm: scanned {scanned:,}, {count:,} matched ({time.monotonic() - t:.0f}s)...')
                try:
                    src_cid = int(row[i_src_cid])
                    tgt_cid = int(row[i_tgt_cid])
                except (ValueError, IndexError):
                    continue
                if src_cid not in loaded_ids or tgt_cid not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    rows.append((
                        row[i_src_code][:50],
                        src_cid,
                        row[i_src_vid][:20],
                        (row[i_src_desc] or None) if i_src_desc < len(row) else None,
                        tgt_cid,
                        row[i_tgt_vid][:20],
                        _parse_date(row[i_start]),
                        _parse_date(row[i_end]),
                        row[i_inv][:1] if row[i_inv] else None,
                    ))
                    if len(rows) >= BATCH:
                        _copy_rows('source_to_concept_map', cols_out, rows, self._log, direct=self._direct)
                        rows = []
        if not dry_run:
            _copy_rows('source_to_concept_map', cols_out, rows, self._log, direct=self._direct)
        self._cleanup('SOURCE_TO_CONCEPT_MAP.csv')
        self._log(f'  stcm: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _publish_release(self, counts):
        """Create a published VocabularyRelease row capturing this load."""
        from datetime import datetime as dt, timezone as _tz
        from django.utils import timezone

        vocab_versions = dict(
            Vocabulary.objects.order_by('vocabulary_id')
            .values_list('vocabulary_id', 'vocabulary_version')
        )
        # row_counts must reflect the ACTUAL table content the snapshot streams
        # (SELECT COUNT(*)), not this run's load counts — the table can hold rows
        # from prior loads / seed data / metadata vocabularies, so a consumer that
        # cross-checks streamed rows against the manifest (EXACT's fail-closed
        # completeness gate) would otherwise always mismatch.
        checksums = {}
        real_counts = {}
        for table_name in counts:
            try:
                with connection.cursor() as cur:
                    cur.execute(
                        f'SELECT COUNT(*), MIN(ctid::text), MAX(ctid::text) '
                        f'FROM {table_name}'
                    )
                    row = cur.fetchone()
                    real_counts[table_name] = row[0]
                    checksums[table_name] = {
                        'count': row[0],
                        'min_ctid': row[1],
                        'max_ctid': row[2],
                    }
            except Exception as exc:
                # Fall back to this run's load count, but never silently: a bare
                # swallow here reintroduces the manifest↔stream mismatch (#343)
                # with no server-side signal for the consumer's fail-closed gate.
                logger.warning(
                    "vocabulary_release: COUNT(*)/ctid probe failed for %s (%s); "
                    "falling back to this run's load count — the manifest may "
                    "disagree with the streamed table for %s.",
                    table_name, exc, table_name, exc_info=True,
                )
                real_counts[table_name] = counts[table_name]
                checksums[table_name] = {'count': counts[table_name]}

        now = timezone.now()
        build_ts = dt.fromtimestamp(self._build_start, tz=_tz.utc)
        release = VocabularyRelease.objects.create(
            schema_version='5.4',
            scope=sorted(VOCAB_SCOPE),
            build_timestamp=build_ts,
            athena_version=getattr(self, '_cdm_vocab_version', None),
            vocab_versions=vocab_versions,
            row_counts=real_counts,
            checksums=checksums,
            status='published',
            published_at=now,
        )
        self._log(f'  vocabulary_release: published release pk={release.pk}')
