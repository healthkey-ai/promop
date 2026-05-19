import csv
import os
import time
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import (
    Vocabulary, Domain, ConceptClass, Concept,
    Relationship, ConceptRelationship, ConceptAncestor,
)

VOCAB_SCOPE = frozenset({'HemOnc', 'RxNorm', 'RxNorm Extension', 'ATC'})
RXNORM_CLASS_SCOPE = frozenset({'Ingredient', 'Clinical Drug', 'Branded Drug', 'Clinical Drug Comp'})
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


def _concept_in_scope(row):
    vid = row['vocabulary_id']
    if vid not in VOCAB_SCOPE:
        return False
    if vid == 'ATC':
        return row['concept_code'].startswith('L')
    if vid in ('RxNorm', 'RxNorm Extension'):
        return row['concept_class_id'] in RXNORM_CLASS_SCOPE
    return True  # HemOnc: all


def _bulk(model, batch, dry_run):
    if not dry_run and batch:
        model.objects.bulk_create(batch, ignore_conflicts=True)


class Command(BaseCommand):
    help = 'Load OHDSI Athena vocabulary TSV files into OMOP vocabulary tables'

    def add_arguments(self, parser):
        parser.add_argument('--path', required=True,
                            help='Directory containing Athena TSV files')
        parser.add_argument('--replace', action='store_true',
                            help='Clear vocabulary rows before loading')
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Count rows without writing to DB')

    def handle(self, *args, **options):
        base = options['path']
        replace = options['replace']
        dry_run = options['dry_run']
        t0 = time.monotonic()

        if replace and not dry_run:
            self._clear()
            self.stdout.write('Cleared existing vocabulary data.')

        counts = {
            'relationship':         self._load_relationships(base, dry_run),
            'vocabulary':           self._load_small(base, 'VOCABULARY.csv', dry_run,
                                        self._vocab_row),
            'domain':               self._load_small(base, 'DOMAIN.csv', dry_run,
                                        self._domain_row),
            'concept_class':        self._load_small(base, 'CONCEPT_CLASS.csv', dry_run,
                                        self._cc_row),
            'concept':              self._load_concepts(base, dry_run),
            'concept_relationship': self._load_concept_relationships(base, dry_run),
            'concept_ancestor':     self._load_concept_ancestors(base, dry_run),
        }
        verb = 'would load' if dry_run else 'loaded'
        for table, n in counts.items():
            self.stdout.write(f'  {table}: {n} rows {verb}')
        self.stdout.write(f'Done in {time.monotonic() - t0:.1f}s')

    def _clear(self):
        ConceptAncestor.objects.all().delete()
        ConceptRelationship.objects.all().delete()
        Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE).delete()
        Relationship.objects.all().delete()

    def _load_relationships(self, base, dry_run):
        count = 0
        batch = []
        with _open_tsv(base, 'RELATIONSHIP.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                if not dry_run:
                    batch.append(Relationship(
                        relationship_id=row['relationship_id'][:20],
                        relationship_name=row['relationship_name'][:255],
                        is_hierarchical=int(row['is_hierarchical'] or 0),
                        defines_ancestry=int(row['defines_ancestry'] or 0),
                        reverse_relationship_id=row['reverse_relationship_id'][:20],
                        relationship_concept_id=int(row['relationship_concept_id'] or 0),
                    ))
                    if len(batch) >= BATCH:
                        _bulk(Relationship, batch, dry_run)
                        batch = []
                count += 1
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

    def _load_small(self, base, filename, dry_run, row_fn):
        """Generic loader for small lookup tables (Vocabulary, Domain, ConceptClass)."""
        count = 0
        batch = []
        model = None
        try:
            f = _open_tsv(base, filename)
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

    def _load_concepts(self, base, dry_run):
        count = 0
        batch = []
        with _open_tsv(base, 'CONCEPT.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                if not _concept_in_scope(row):
                    continue
                if not dry_run:
                    batch.append(Concept(
                        concept_id=int(row['concept_id']),
                        concept_name=row['concept_name'][:255],
                        domain_id=row['domain_id'][:20],
                        vocabulary_id=row['vocabulary_id'][:20],
                        concept_class_id=row['concept_class_id'][:20],
                        standard_concept=row['standard_concept'][:1] if row['standard_concept'] else None,
                        concept_code=row['concept_code'][:50],
                        valid_start_date=_parse_date(row['valid_start_date']),
                        valid_end_date=_parse_date(row['valid_end_date']),
                        invalid_reason=row['invalid_reason'][:1] if row.get('invalid_reason') else None,
                    ))
                    if len(batch) >= BATCH:
                        _bulk(Concept, batch, dry_run)
                        batch = []
                count += 1
        _bulk(Concept, batch, dry_run)
        return count

    def _load_concept_relationships(self, base, dry_run):
        loaded_ids = (
            set(Concept.objects.filter(vocabulary_id__in=VOCAB_SCOPE)
                               .values_list('concept_id', flat=True))
            if not dry_run else set()
        )
        count = 0
        batch = []
        with _open_tsv(base, 'CONCEPT_RELATIONSHIP.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                c1 = int(row['concept_id_1'])
                c2 = int(row['concept_id_2'])
                if not dry_run and (c1 not in loaded_ids or c2 not in loaded_ids):
                    continue
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
                count += 1
        _bulk(ConceptRelationship, batch, dry_run)
        return count

    def _load_concept_ancestors(self, base, dry_run):
        hemonc_ids = (
            set(Concept.objects.filter(vocabulary_id='HemOnc')
                               .values_list('concept_id', flat=True))
            if not dry_run else set()
        )
        count = 0
        batch = []
        with _open_tsv(base, 'CONCEPT_ANCESTOR.csv') as f:
            for row in csv.DictReader(f, delimiter='\t'):
                anc = int(row['ancestor_concept_id'])
                desc = int(row['descendant_concept_id'])
                if not dry_run and (anc not in hemonc_ids or desc not in hemonc_ids):
                    continue
                if not dry_run:
                    batch.append(ConceptAncestor(
                        ancestor_concept_id=anc,
                        descendant_concept_id=desc,
                        min_levels_of_separation=int(row['min_levels_of_separation']),
                        max_levels_of_separation=int(row['max_levels_of_separation']),
                    ))
                    if len(batch) >= BATCH:
                        _bulk(ConceptAncestor, batch, dry_run)
                        batch = []
                count += 1
        _bulk(ConceptAncestor, batch, dry_run)
        return count
