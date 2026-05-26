import csv
import os
import sys
import time
from datetime import date
from pathlib import Path

csv.field_size_limit(sys.maxsize)

from django.core.management.base import BaseCommand, CommandError

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
BATCH = 1000


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


def _download_gcs_blob(bucket, filename, stdout):
    blob = bucket.blob(filename)
    if not blob.exists():
        raise CommandError(f'Required blob not found: gs://{bucket.name}/{filename}')
    dest = Path('/tmp/vocab') / filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    size_mb = (blob.size or 0) / 1048576
    stdout.write(f'  Downloading {filename} ({size_mb:.0f}MB)...')
    blob.download_to_filename(str(dest))
    stdout.write(f'  Downloaded {filename}.')
    return open(dest, encoding='utf-8', newline='')


def _concept_in_scope(row):
    vid = row['vocabulary_id']
    if vid not in VOCAB_SCOPE:
        return False
    if vid == 'ATC':
        return row['concept_code'].startswith('L')
    if vid in ('RxNorm', 'RxNorm Extension'):
        return row['concept_class_id'] in RXNORM_CLASS_SCOPE
    if vid == 'LOINC':
        return row['domain_id'] in LOINC_DOMAIN_SCOPE
    return True  # HemOnc, UCUM: all


def _bulk(model, batch, dry_run):
    if not dry_run and batch:
        model.objects.bulk_create(batch, ignore_conflicts=True)


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
            self.stdout.write(f'Loading from gs://{bucket_name}/ (download-one-process-delete)')

        t0 = time.monotonic()

        self._base = base

        if replace and not dry_run:
            self._clear()
            self.stdout.write('Cleared existing vocabulary data.')

        counts = {
            'relationship':         self._load_relationships(dry_run),
            'vocabulary':           self._load_small('VOCABULARY.csv', dry_run,
                                        self._vocab_row),
            'domain':               self._load_small('DOMAIN.csv', dry_run,
                                        self._domain_row),
            'concept_class':        self._load_small('CONCEPT_CLASS.csv', dry_run,
                                        self._cc_row),
            'concept':              self._load_concepts(dry_run),
            'concept_relationship': self._load_concept_relationships(dry_run),
            'concept_ancestor':     self._load_concept_ancestors(dry_run),
        }
        verb = 'would load' if dry_run else 'loaded'
        for table, n in counts.items():
            self.stdout.write(f'  {table}: {n} rows {verb}')
        self.stdout.write(f'Done in {time.monotonic() - t0:.1f}s')

    def _open(self, filename):
        if self._gcs_bucket:
            return _download_gcs_blob(self._gcs_bucket, filename, self.stdout)
        return _open_tsv(self._base, filename)

    def _cleanup(self, filename):
        if self._gcs_bucket:
            tmp = Path('/tmp/vocab') / filename
            if tmp.exists():
                tmp.unlink()
                self.stdout.write(f'  Cleaned up {filename}.')

    def _clear(self):
        ConceptAncestor.objects.all().delete()
        ConceptRelationship.objects.all().delete()
        Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE).delete()
        Relationship.objects.all().delete()
        Vocabulary.objects.filter(vocabulary_id__in=VOCAB_SCOPE).delete()

    def _load_relationships(self, dry_run):
        count = 0
        batch = []
        with self._open('RELATIONSHIP.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                try:
                    obj = Relationship(
                        relationship_id=row['relationship_id'][:20],
                        relationship_name=row['relationship_name'][:255],
                        is_hierarchical=int(row['is_hierarchical'] or 0),
                        defines_ancestry=int(row['defines_ancestry'] or 0),
                        reverse_relationship_id=row['reverse_relationship_id'][:20],
                        relationship_concept_id=int(row['relationship_concept_id'] or 0),
                    )
                except (ValueError, KeyError) as exc:
                    self.stderr.write(f'Warning: skipping malformed relationship row: {exc}')
                    continue
                count += 1
                if not dry_run:
                    batch.append(obj)
                    if len(batch) >= BATCH:
                        _bulk(Relationship, batch, dry_run)
                        batch = []
        _bulk(Relationship, batch, dry_run)
        return count

    def _vocab_row(self, row):
        if row['vocabulary_id'] not in VOCAB_SCOPE:
            return None
        return Vocabulary(
            vocabulary_id=row['vocabulary_id'][:20],
            vocabulary_name=row['vocabulary_name'][:255],
            vocabulary_reference=(row.get('vocabulary_reference') or '')[:255],
            vocabulary_version=(row.get('vocabulary_version') or '')[:255],
            vocabulary_concept_id=int(row.get('vocabulary_concept_id') or 0),
        )

    def _domain_row(self, row):
        return Domain(
            domain_id=row['domain_id'][:20],
            domain_name=row['domain_name'][:255],
            domain_concept_id=int(row.get('domain_concept_id') or 0),
        )

    def _cc_row(self, row):
        return ConceptClass(
            concept_class_id=row['concept_class_id'][:20],
            concept_class_name=row['concept_class_name'][:255],
            concept_class_concept_id=int(row.get('concept_class_concept_id') or 0),
        )

    def _load_small(self, filename, dry_run, row_fn):
        """Generic loader for small lookup tables (Vocabulary, Domain, ConceptClass)."""
        count = 0
        batch = []
        model = None
        try:
            f = self._open(filename)
        except CommandError:
            return 0
        with f:
            for row in csv.DictReader(f, delimiter='\t'):
                obj = row_fn(row)
                if obj is None:
                    continue
                if model is None:
                    model = type(obj)
                if not dry_run:
                    batch.append(obj)
                    if len(batch) >= BATCH:
                        model.objects.bulk_create(batch, ignore_conflicts=True)
                        batch = []
                count += 1
        if model and batch and not dry_run:
            model.objects.bulk_create(batch, ignore_conflicts=True)
        return count

    def _load_concepts(self, dry_run):
        count = 0
        batch = []
        with self._open('CONCEPT.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                if not _concept_in_scope(row):
                    continue
                try:
                    concept_id = int(row['concept_id'])
                    start = _parse_date(row['valid_start_date'])
                    end = _parse_date(row['valid_end_date'])
                except (ValueError, KeyError) as exc:
                    self.stderr.write(f'Warning: skipping malformed concept row: {exc}')
                    continue
                count += 1
                if not dry_run:
                    batch.append(Concept(
                        concept_id=concept_id,
                        concept_name=row['concept_name'][:255],
                        domain_id=row['domain_id'][:20],
                        vocabulary_id=row['vocabulary_id'][:20],
                        concept_class_id=row['concept_class_id'][:20],
                        standard_concept=row['standard_concept'][:1] if row['standard_concept'] else None,
                        concept_code=row['concept_code'][:50],
                        valid_start_date=start,
                        valid_end_date=end,
                        invalid_reason=row['invalid_reason'][:1] if row.get('invalid_reason') else None,
                    ))
                    if len(batch) >= BATCH:
                        _bulk(Concept, batch, dry_run)
                        batch = []
        _bulk(Concept, batch, dry_run)
        self._cleanup('CONCEPT.csv')
        return count

    def _load_concept_relationships(self, dry_run):
        loaded_ids = set(
            Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE)
                           .values_list('concept_id', flat=True)
        )
        count = 0
        batch = []
        with self._open('CONCEPT_RELATIONSHIP.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                try:
                    c1 = int(row['concept_id_1'])
                    c2 = int(row['concept_id_2'])
                except (ValueError, KeyError):
                    continue
                if c1 not in loaded_ids or c2 not in loaded_ids:
                    continue
                count += 1
                if not dry_run:
                    batch.append(ConceptRelationship(
                        concept_1_id=c1,
                        concept_2_id=c2,
                        relationship_id=row['relationship_id'][:20],
                        valid_start_date=_parse_date(row['valid_start_date']),
                        valid_end_date=_parse_date(row['valid_end_date']),
                        invalid_reason=row['invalid_reason'][:1] if row.get('invalid_reason') else None,
                    ))
                    if len(batch) >= BATCH:
                        _bulk(ConceptRelationship, batch, dry_run)
                        batch = []
        _bulk(ConceptRelationship, batch, dry_run)
        self._cleanup('CONCEPT_RELATIONSHIP.csv')
        return count

    def _load_concept_ancestors(self, dry_run):
        hemonc_ids = set(
            Concept.objects.filter(vocabulary_id='HemOnc')
                           .values_list('concept_id', flat=True)
        )
        count = 0
        batch = []
        with self._open('CONCEPT_ANCESTOR.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                try:
                    anc = int(row['ancestor_concept_id'])
                    desc = int(row['descendant_concept_id'])
                except (ValueError, KeyError):
                    continue
                if anc not in hemonc_ids or desc not in hemonc_ids:
                    continue
                count += 1
                if not dry_run:
                    try:
                        min_sep = int(row['min_levels_of_separation'])
                        max_sep = int(row['max_levels_of_separation'])
                    except (ValueError, KeyError):
                        continue
                    batch.append(ConceptAncestor(
                        ancestor_concept_id=anc,
                        descendant_concept_id=desc,
                        min_levels_of_separation=min_sep,
                        max_levels_of_separation=max_sep,
                    ))
                    if len(batch) >= BATCH:
                        _bulk(ConceptAncestor, batch, dry_run)
                        batch = []
        _bulk(ConceptAncestor, batch, dry_run)
        self._cleanup('CONCEPT_ANCESTOR.csv')
        return count
