import csv
import sys
import time
from datetime import date
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from omop_core.models import (
    Vocabulary, Domain, ConceptClass, Concept,
    Relationship, ConceptRelationship, ConceptAncestor,
)

VOCAB_SCOPE = frozenset({
    'HemOnc', 'RxNorm', 'RxNorm Extension', 'ATC', 'LOINC', 'UCUM',
    'Visit', 'Type Concept',
})
RXNORM_CLASS_SCOPE = frozenset({'Ingredient', 'Clinical Drug', 'Branded Drug', 'Clinical Drug Comp'})
LOINC_DOMAIN_SCOPE = frozenset({'Measurement', 'Observation'})
BATCH = 5000
PROGRESS_EVERY = 500_000


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


def _copy_rows(table, columns, rows, log):
    """Use PostgreSQL COPY into temp table, then upsert to handle duplicates."""
    if not rows:
        return
    connection.ensure_connection()
    cols = ', '.join(columns)
    tmp = f'_tmp_{table}'
    with connection.connection.cursor() as cur:
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

    def handle(self, *args, **options):
        base = options['path']
        bucket_name = options['bucket']
        replace = options['replace']
        dry_run = options['dry_run']

        if not base and not bucket_name:
            raise CommandError('Provide either --path or --bucket')

        self._gcs_bucket = None
        if bucket_name:
            from google.cloud import storage as gcs
            self._gcs_bucket = gcs.Client().bucket(bucket_name)
            self._log(f'Loading from gs://{bucket_name}/ (download-one-process-delete)')

        t0 = time.monotonic()

        self._base = base

        if replace and not dry_run:
            self._clear()

        counts = {
            'relationship':         self._load_relationships(dry_run),
            'vocabulary':           self._load_vocabularies(dry_run),
            'domain':               self._load_domains(dry_run),
            'concept_class':        self._load_concept_classes(dry_run),
            'concept':              self._load_concepts(dry_run),
            'concept_relationship': self._load_concept_relationships(dry_run),
            'concept_ancestor':     self._load_concept_ancestors(dry_run),
        }
        verb = 'would load' if dry_run else 'loaded'
        for table, n in counts.items():
            self._log(f'  {table}: {n:,} rows {verb}')
        self._log(f'Done in {time.monotonic() - t0:.0f}s')

    def _log(self, msg):
        self.stdout.write(msg)
        self.stdout.flush()

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
                'concept, relationship, vocabulary CASCADE'
            )
        self._log(f'  Truncated all vocab tables in {time.monotonic() - t:.0f}s')

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
                                   rows, self._log)
                        rows = []
        if not dry_run:
            _copy_rows('relationship',
                       ('relationship_id', 'relationship_name', 'is_hierarchical',
                        'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'),
                       rows, self._log)
        self._cleanup('RELATIONSHIP.csv')
        self._log(f'  RELATIONSHIP.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_vocabularies(self, dry_run):
        self._log('Loading VOCABULARY.csv...')
        t = time.monotonic()
        count = 0
        rows = []
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
                if vid not in VOCAB_SCOPE:
                    continue
                count += 1
                if not dry_run:
                    rows.append((
                        vid[:20],
                        cols[idx['vocabulary_name']][:255],
                        (cols[idx.get('vocabulary_reference', -1)] if 'vocabulary_reference' in idx else '')[:255] or '',
                        (cols[idx.get('vocabulary_version', -1)] if 'vocabulary_version' in idx else '')[:255] or '',
                        int(cols[idx['vocabulary_concept_id']] or 0) if 'vocabulary_concept_id' in idx else 0,
                    ))
        if not dry_run and rows:
            _copy_rows('vocabulary',
                       ('vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
                        'vocabulary_version', 'vocabulary_concept_id'),
                       rows, self._log)
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
                       rows, self._log)
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
                       rows, self._log)
        self._cleanup('CONCEPT_CLASS.csv')
        self._log(f'  CONCEPT_CLASS.csv: {count:,} rows in {time.monotonic() - t:.0f}s')
        return count

    def _load_concepts(self, dry_run):
        self._log('Loading CONCEPT.csv...')
        t = time.monotonic()
        count = 0
        scanned = 0
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
                                   rows, self._log)
                        rows = []
        if not dry_run:
            _copy_rows('concept',
                       ('concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
                        'concept_class_id', 'standard_concept', 'concept_code',
                        'valid_start_date', 'valid_end_date', 'invalid_reason'),
                       rows, self._log)
        self._cleanup('CONCEPT.csv')
        self._log(f'  concepts: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
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
                                   rows, self._log)
                        rows = []
        if not dry_run:
            _copy_rows('concept_relationship',
                       ('concept_id_1', 'concept_id_2', 'relationship_id',
                        'valid_start_date', 'valid_end_date', 'invalid_reason'),
                       rows, self._log)
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
                                   rows, self._log)
                        rows = []
        if not dry_run:
            _copy_rows('concept_ancestor',
                       ('ancestor_concept_id', 'descendant_concept_id',
                        'min_levels_of_separation', 'max_levels_of_separation'),
                       rows, self._log)
        self._cleanup('CONCEPT_ANCESTOR.csv')
        self._log(f'  ancestors: {count:,} loaded from {scanned:,} rows in {time.monotonic() - t:.0f}s')
        return count
