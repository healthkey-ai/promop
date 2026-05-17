# Athena Vocabulary Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load OHDSI Athena vocabulary files (HemOnc, RxNorm oncology subset) into the ctomop PostgreSQL database and add a RxNav API service that resolves unknown drug names at FHIR upload time.

**Architecture:** Three new OMOP-standard models (`Relationship`, `ConceptRelationship`, `ConceptAncestor`) are added to `omop_core/models.py`. A management command `load_athena_vocabularies` bulk-loads filtered Athena TSV files. A `rxnav_service` calls the free NIH RxNav API for drugs not found locally and caches results as `Concept` rows. The FHIR upload wires in `rxnav_service` so drug concepts are resolved at ingest time.

**Tech Stack:** Django 5.x, psycopg3, OHDSI OMOP CDM v5.4, RxNav REST API (rxnav.nlm.nih.gov — free, no auth)

**Run all tests with:**
```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test \
    patient_portal.tests.VocabularyRelationshipModelTest \
    patient_portal.tests.AthenaVocabularyLoadTest \
    patient_portal.tests.RxNavServiceTest \
    patient_portal.tests.FhirRxNavIntegrationTest \
    --no-input 2>&1 | tail -15
```

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `omop_core/models.py` | Add `Relationship`, `ConceptRelationship`, `ConceptAncestor` after the `Concept` class |
| Create | `omop_core/migrations/0065_add_vocabulary_relationship_tables.py` | Schema migration (via `makemigrations`) |
| Create | `omop_core/management/commands/load_athena_vocabularies.py` | Bulk-load Athena TSV files with vocabulary filtering |
| Create | `omop_core/services/rxnav_service.py` | RxNav API lookup + Concept caching |
| Modify | `patient_portal/api/views.py` | Import `resolve_drug`; call it after regimen concept lookup fails (line ~1365) |
| Modify | `patient_portal/tests.py` | Add four new test classes |

---

## Task 1: Add vocabulary relationship models + migration

**Files:**
- Modify: `omop_core/models.py`
- Create: `omop_core/migrations/0065_add_vocabulary_relationship_tables.py` (via makemigrations)
- Test: `patient_portal/tests.py`

- [ ] **Step 1: Write the failing tests**

Add this class to `patient_portal/tests.py` (after the existing imports, before or after any existing class — it extends plain `TestCase`):

```python
class VocabularyRelationshipModelTest(TestCase):
    """Verify Relationship, ConceptRelationship, ConceptAncestor models exist and are queryable."""

    def setUp(self):
        _make_vocab_fixtures()
        vocab = Vocabulary.objects.get(vocabulary_id='TEST')
        domain = Domain.objects.get(domain_id='Drug')
        cc = ConceptClass.objects.get(concept_class_id='Clinical Finding')
        self.c1 = Concept.objects.create(
            concept_id=9901001, concept_name='Drug A',
            domain=domain, vocabulary=vocab, concept_class=cc,
            concept_code='A1',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.c2 = Concept.objects.create(
            concept_id=9901002, concept_name='Drug Class B',
            domain=domain, vocabulary=vocab, concept_class=cc,
            concept_code='B1',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

    def test_relationship_model(self):
        from omop_core.models import Relationship
        Relationship.objects.create(
            relationship_id='test-maps-to',
            relationship_name='Test Maps To',
            is_hierarchical='0',
            defines_ancestry='0',
            reverse_relationship_id='test-mapped-from',
            relationship_concept_id=0,
        )
        self.assertEqual(
            Relationship.objects.get(pk='test-maps-to').relationship_name,
            'Test Maps To',
        )

    def test_concept_relationship_model(self):
        from omop_core.models import Relationship, ConceptRelationship
        r = Relationship.objects.create(
            relationship_id='Maps to',
            relationship_name='Maps to',
            is_hierarchical='0',
            defines_ancestry='0',
            reverse_relationship_id='Mapped from',
            relationship_concept_id=0,
        )
        ConceptRelationship.objects.create(
            concept_1=self.c1,
            concept_2=self.c2,
            relationship=r,
            valid_start_date=date(1970, 1, 1),
            valid_end_date=date(2099, 12, 31),
        )
        self.assertEqual(
            ConceptRelationship.objects.filter(concept_1=self.c1).count(), 1
        )

    def test_concept_ancestor_model(self):
        from omop_core.models import ConceptAncestor
        ConceptAncestor.objects.create(
            ancestor_concept=self.c2,
            descendant_concept=self.c1,
            min_levels_of_separation=1,
            max_levels_of_separation=1,
        )
        self.assertEqual(
            ConceptAncestor.objects.filter(descendant_concept=self.c1).count(), 1
        )

    def test_unique_together_concept_relationship(self):
        from omop_core.models import Relationship, ConceptRelationship
        from django.db import IntegrityError
        r = Relationship.objects.create(
            relationship_id='Is a',
            relationship_name='Is a',
            is_hierarchical='1',
            defines_ancestry='1',
            reverse_relationship_id='Subsumes',
            relationship_concept_id=0,
        )
        ConceptRelationship.objects.create(
            concept_1=self.c1, concept_2=self.c2, relationship=r,
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        with self.assertRaises(IntegrityError):
            ConceptRelationship.objects.create(
                concept_1=self.c1, concept_2=self.c2, relationship=r,
                valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
            )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.VocabularyRelationshipModelTest --no-input 2>&1 | tail -5
```
Expected: `ImportError` or `AttributeError` (models don't exist yet)

- [ ] **Step 3: Add the three models to omop_core/models.py**

Find the `Concept` class in `omop_core/models.py` (around line 110). Insert the following three classes **immediately after** the `Concept` class closing line and before the `Location` class:

```python
class Relationship(models.Model):
    """OMOP CDM Relationship table — metadata about relationship types."""
    relationship_id = models.CharField(max_length=20, primary_key=True)
    relationship_name = models.CharField(max_length=255)
    is_hierarchical = models.CharField(max_length=1)
    defines_ancestry = models.CharField(max_length=1)
    reverse_relationship_id = models.CharField(max_length=20)
    relationship_concept_id = models.IntegerField()

    class Meta:
        db_table = 'relationship'

    def __str__(self):
        return self.relationship_id


class ConceptRelationship(models.Model):
    """OMOP CDM ConceptRelationship table — directed edges between concepts."""
    concept_1 = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='relationships_as_source', db_column='concept_id_1',
    )
    concept_2 = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='relationships_as_target', db_column='concept_id_2',
    )
    relationship = models.ForeignKey(
        Relationship, on_delete=models.DO_NOTHING, db_column='relationship_id',
    )
    valid_start_date = models.DateField()
    valid_end_date = models.DateField()
    invalid_reason = models.CharField(max_length=1, null=True, blank=True)

    class Meta:
        db_table = 'concept_relationship'
        unique_together = [('concept_1', 'concept_2', 'relationship')]

    def __str__(self):
        return f'{self.concept_1_id} --[{self.relationship_id}]--> {self.concept_2_id}'


class ConceptAncestor(models.Model):
    """OMOP CDM ConceptAncestor table — pre-computed ancestry within HemOnc hierarchy."""
    ancestor_concept = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='descendants', db_column='ancestor_concept_id',
    )
    descendant_concept = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='ancestors', db_column='descendant_concept_id',
    )
    min_levels_of_separation = models.IntegerField()
    max_levels_of_separation = models.IntegerField()

    class Meta:
        db_table = 'concept_ancestor'
        unique_together = [('ancestor_concept', 'descendant_concept')]

    def __str__(self):
        return f'{self.ancestor_concept_id} -> {self.descendant_concept_id}'
```

- [ ] **Step 4: Generate and apply the migration**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py makemigrations omop_core --name add_vocabulary_relationship_tables
```
Expected: creates `omop_core/migrations/0065_add_vocabulary_relationship_tables.py`

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py migrate
```
Expected: `Applying omop_core.0065_add_vocabulary_relationship_tables... OK`

- [ ] **Step 5: Run tests to verify they pass**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.VocabularyRelationshipModelTest --no-input 2>&1 | tail -5
```
Expected: `Ran 4 tests ... OK`

- [ ] **Step 6: Commit**

```bash
git add omop_core/models.py omop_core/migrations/0065_add_vocabulary_relationship_tables.py patient_portal/tests.py
git commit -m "feat: add Relationship, ConceptRelationship, ConceptAncestor OMOP vocabulary models"
```

---

## Task 2: Create load_athena_vocabularies management command

**Files:**
- Create: `omop_core/management/commands/load_athena_vocabularies.py`
- Modify: `patient_portal/tests.py`

**Background:** Athena TSV files use tab separators despite `.csv` extension. Date fields are in `YYYYMMDD` format (8-digit integers). Column headers match OMOP CDM field names. The load command must filter concepts to vocabulary scope and use batched `bulk_create` with `ignore_conflicts=True`.

- [ ] **Step 1: Write the failing tests**

Add this class to `patient_portal/tests.py`:

```python
import os
import tempfile

class AthenaVocabularyLoadTest(TestCase):
    """Test load_athena_vocabularies management command with minimal fixture TSV files."""

    def _write_tsv(self, directory, filename, headers, rows):
        path = os.path.join(directory, filename)
        with open(path, 'w', newline='') as f:
            f.write('\t'.join(headers) + '\n')
            for row in rows:
                f.write('\t'.join(str(v) for v in row) + '\n')

    def _write_minimal_athena(self, directory):
        """Write the minimal set of Athena TSV files needed for tests."""
        self._write_tsv(directory, 'RELATIONSHIP.csv',
            ['relationship_id', 'relationship_name', 'is_hierarchical',
             'defines_ancestry', 'reverse_relationship_id', 'relationship_concept_id'],
            [['Maps to', 'Maps to value', '0', '0', 'Mapped from', '44818965'],
             ['Is a', 'Is a', '1', '1', 'Subsumes', '44818723']],
        )
        self._write_tsv(directory, 'VOCABULARY.csv',
            ['vocabulary_id', 'vocabulary_name', 'vocabulary_reference',
             'vocabulary_version', 'vocabulary_concept_id'],
            [['HemOnc', 'HemOnc Oncology', '', 'v2024', '0'],
             ['RxNorm', 'RxNorm', '', '2024AA', '0'],
             ['SNOMED', 'SNOMED CT', '', '2024', '0']],  # should be skipped
        )
        self._write_tsv(directory, 'DOMAIN.csv',
            ['domain_id', 'domain_name', 'domain_concept_id'],
            [['Drug', 'Drug', '13']],
        )
        self._write_tsv(directory, 'CONCEPT_CLASS.csv',
            ['concept_class_id', 'concept_class_name', 'concept_class_concept_id'],
            [['HemOnc Class', 'HemOnc Class', '0'],
             ['Ingredient', 'Ingredient', '0']],
        )
        self._write_tsv(directory, 'CONCEPT.csv',
            ['concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
             'concept_class_id', 'standard_concept', 'concept_code',
             'valid_start_date', 'valid_end_date', 'invalid_reason'],
            # HemOnc concepts — should be loaded
            [['5000001', 'Proteasome inhibitor', 'Drug', 'HemOnc', 'HemOnc Class', 'S', 'PI', '19700101', '20991231', ''],
             ['5000002', 'bortezomib',           'Drug', 'HemOnc', 'HemOnc Class', 'S', 'HO-Bort', '19700101', '20991231', ''],
             # RxNorm Ingredient — should be loaded
             ['5000003', 'bortezomib',           'Drug', 'RxNorm', 'Ingredient', 'S', '1421', '19700101', '20991231', ''],
             # RxNorm Branded — should be loaded
             ['5000004', 'Velcade',              'Drug', 'RxNorm', 'Branded Drug', 'S', '213269', '19700101', '20991231', ''],
             # SNOMED concept — should be SKIPPED (not in vocabulary scope)
             ['5000099', 'Some SNOMED concept',  'Condition', 'SNOMED', 'Clinical Finding', 'S', '123456', '19700101', '20991231', '']],
        )
        self._write_tsv(directory, 'CONCEPT_RELATIONSHIP.csv',
            ['concept_id_1', 'concept_id_2', 'relationship_id',
             'valid_start_date', 'valid_end_date', 'invalid_reason'],
            # RxNorm bortezomib → HemOnc bortezomib (both in scope)
            [['5000003', '5000002', 'Maps to', '19700101', '20991231', ''],
             # Edge to out-of-scope SNOMED concept — should be SKIPPED
             ['5000003', '5000099', 'Maps to', '19700101', '20991231', '']],
        )
        self._write_tsv(directory, 'CONCEPT_ANCESTOR.csv',
            ['ancestor_concept_id', 'descendant_concept_id',
             'min_levels_of_separation', 'max_levels_of_separation'],
            # HemOnc: PI class is ancestor of bortezomib HemOnc concept
            [['5000001', '5000002', '1', '1'],
             # Edge referencing out-of-scope concept — should be SKIPPED
             ['5000001', '5000099', '2', '2']],
        )

    def test_load_creates_relationship_rows(self):
        from omop_core.models import Relationship
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        self.assertTrue(Relationship.objects.filter(relationship_id='Maps to').exists())
        self.assertTrue(Relationship.objects.filter(relationship_id='Is a').exists())

    def test_load_filters_concepts_to_scope(self):
        from omop_core.models import Concept
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        self.assertTrue(Concept.objects.filter(concept_id=5000001).exists())  # HemOnc
        self.assertTrue(Concept.objects.filter(concept_id=5000003).exists())  # RxNorm Ingredient
        self.assertTrue(Concept.objects.filter(concept_id=5000004).exists())  # RxNorm Branded
        self.assertFalse(Concept.objects.filter(concept_id=5000099).exists())  # SNOMED — excluded

    def test_load_filters_concept_relationships(self):
        from omop_core.models import ConceptRelationship
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        # Edge between two in-scope concepts should be loaded
        self.assertTrue(ConceptRelationship.objects.filter(
            concept_1_id=5000003, concept_2_id=5000002
        ).exists())
        # Edge to out-of-scope SNOMED concept should be skipped
        self.assertFalse(ConceptRelationship.objects.filter(
            concept_2_id=5000099
        ).exists())

    def test_load_concept_ancestors_hemonc_only(self):
        from omop_core.models import ConceptAncestor
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
        self.assertTrue(ConceptAncestor.objects.filter(
            ancestor_concept_id=5000001, descendant_concept_id=5000002
        ).exists())
        # Out-of-scope ancestor edge should be skipped
        self.assertFalse(ConceptAncestor.objects.filter(
            descendant_concept_id=5000099
        ).exists())

    def test_idempotent_reload(self):
        from omop_core.models import Concept
        from django.core.management import call_command
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir)
            count_after_first = Concept.objects.filter(vocabulary_id='HemOnc').count()
            call_command('load_athena_vocabularies', path=tmpdir)
            count_after_second = Concept.objects.filter(vocabulary_id='HemOnc').count()
        self.assertEqual(count_after_first, count_after_second)

    def test_dry_run_writes_nothing(self):
        from omop_core.models import Concept, Relationship
        from django.core.management import call_command
        before_concepts = Concept.objects.count()
        before_rels = Relationship.objects.count()
        with tempfile.TemporaryDirectory() as tmpdir:
            self._write_minimal_athena(tmpdir)
            call_command('load_athena_vocabularies', path=tmpdir, dry_run=True)
        self.assertEqual(Concept.objects.count(), before_concepts)
        self.assertEqual(Relationship.objects.count(), before_rels)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.AthenaVocabularyLoadTest --no-input 2>&1 | tail -5
```
Expected: errors (command doesn't exist yet)

- [ ] **Step 3: Create the management command**

Create `omop_core/management/commands/load_athena_vocabularies.py`:

```python
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
            'relationship':        self._load_relationships(base, dry_run),
            'vocabulary':          self._load_small(base, 'VOCABULARY.csv', dry_run,
                                       self._vocab_row),
            'domain':              self._load_small(base, 'DOMAIN.csv', dry_run,
                                       self._domain_row),
            'concept_class':       self._load_small(base, 'CONCEPT_CLASS.csv', dry_run,
                                       self._cc_row),
            'concept':             self._load_concepts(base, dry_run),
            'concept_relationship':self._load_concept_relationships(base, dry_run),
            'concept_ancestor':    self._load_concept_ancestors(base, dry_run),
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
                        is_hierarchical=row['is_hierarchical'][:1],
                        defines_ancestry=row['defines_ancestry'][:1],
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.AthenaVocabularyLoadTest --no-input 2>&1 | tail -5
```
Expected: `Ran 6 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add omop_core/management/commands/load_athena_vocabularies.py patient_portal/tests.py
git commit -m "feat: add load_athena_vocabularies management command — bulk-load Athena TSV files"
```

---

## Task 3: Create rxnav_service

**Files:**
- Create: `omop_core/services/rxnav_service.py`
- Modify: `patient_portal/tests.py`

**Background:** The RxNav REST API (`https://rxnav.nlm.nih.gov/REST`) is a free NIH service requiring no authentication. It resolves drug names to RxNorm CUIs. The key endpoint is `GET /drugs.json?name={name}` which returns concept groups by term type; `tty=IN` entries are active ingredients.

- [ ] **Step 1: Write the failing tests**

Add this class to `patient_portal/tests.py`:

```python
class RxNavServiceTest(TestCase):
    """Test rxnav_service.resolve_drug() with mocked HTTP calls."""

    def setUp(self):
        _make_vocab_fixtures()
        self.vocab_rxnorm, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        self.domain_drug = Domain.objects.get(domain_id='Drug')
        self.cc_ingredient, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )

    def _rxnav_response(self, rxcui, name):
        import json
        return json.dumps({
            'drugGroup': {
                'conceptGroup': [
                    {'tty': 'IN', 'conceptProperties': [{'rxcui': rxcui, 'name': name}]}
                ]
            }
        }).encode()

    def _rxnav_empty(self):
        import json
        return json.dumps({'drugGroup': {'conceptGroup': []}}).encode()

    def _mock_urlopen(self, payload):
        from unittest.mock import MagicMock, patch
        mock_resp = MagicMock()
        mock_resp.read.return_value = payload
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return patch('urllib.request.urlopen', return_value=mock_resp)

    def test_known_drug_returns_existing_concept_without_api_call(self):
        """Drug already in local Concept table → returned without hitting RxNav."""
        from omop_core.services.rxnav_service import resolve_drug
        Concept.objects.create(
            concept_id=9990001, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.vocab_rxnorm,
            concept_class=self.cc_ingredient,
            concept_code='1421', standard_concept='S',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        with self._mock_urlopen(b'should not be called') as mock_open:
            result = resolve_drug('bortezomib')
            mock_open.assert_not_called()
        self.assertEqual(result.concept_id, 9990001)

    def test_unknown_drug_calls_rxnav_and_creates_concept(self):
        """Drug not in local vocab → RxNav called → new Concept row created."""
        from omop_core.services.rxnav_service import resolve_drug
        with self._mock_urlopen(self._rxnav_response('1421', 'bortezomib')):
            result = resolve_drug('Velcade')
        self.assertIsNotNone(result)
        self.assertEqual(result.concept_code, '1421')
        self.assertEqual(result.vocabulary_id, 'RxNorm')
        self.assertTrue(Concept.objects.filter(concept_code='1421', vocabulary_id='RxNorm').exists())

    def test_rxnav_no_results_returns_none(self):
        """RxNav returns no ingredient matches → resolve_drug returns None."""
        from omop_core.services.rxnav_service import resolve_drug
        with self._mock_urlopen(self._rxnav_empty()):
            result = resolve_drug('unknowndrugxyz')
        self.assertIsNone(result)

    def test_rxnav_http_error_returns_none(self):
        """RxNav HTTP error → resolve_drug returns None without raising."""
        from omop_core.services.rxnav_service import resolve_drug
        from unittest.mock import patch
        with patch('urllib.request.urlopen', side_effect=Exception('network error')):
            result = resolve_drug('anything')
        self.assertIsNone(result)

    def test_second_call_uses_cached_concept(self):
        """After first call caches a Concept, second call returns it without API hit."""
        from omop_core.services.rxnav_service import resolve_drug
        with self._mock_urlopen(self._rxnav_response('9876', 'lenalidomide')) as mock_open:
            resolve_drug('Revlimid')
            call_count_after_first = mock_open.call_count
        with self._mock_urlopen(b'should not be called') as mock_open2:
            result = resolve_drug('lenalidomide')
            mock_open2.assert_not_called()
        self.assertIsNotNone(result)
        self.assertEqual(result.concept_code, '9876')
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.RxNavServiceTest --no-input 2>&1 | tail -5
```
Expected: `ImportError` or `ModuleNotFoundError`

- [ ] **Step 3: Create omop_core/services/rxnav_service.py**

```python
import json
import logging
import urllib.parse
import urllib.request
from datetime import date

from omop_core.models import Concept, ConceptClass, Domain, Vocabulary

logger = logging.getLogger('audit')

RXNAV_BASE = 'https://rxnav.nlm.nih.gov/REST'


def resolve_drug(drug_source_value: str):
    """
    Resolve a drug name to an OMOP Concept.

    Checks local Concept table first (RxNorm vocabulary, name or concept_code match).
    Falls back to RxNav API for unknown drugs; caches result as a new Concept row.
    Returns None if neither source can resolve the name. Never raises.
    """
    if not drug_source_value or not drug_source_value.strip():
        return None

    normalized = drug_source_value.strip().lower()

    # Check local vocab by name
    existing = Concept.objects.filter(
        concept_name__iexact=normalized,
        vocabulary_id__in=['RxNorm', 'RxNorm Extension'],
    ).first()
    if existing:
        return existing

    # Try RxNav
    try:
        rxcui, canonical_name = _rxnav_lookup(drug_source_value)
        if not rxcui:
            return None

        # Re-check by concept_code (RXCUI) in case it was loaded from Athena
        existing = Concept.objects.filter(
            concept_code=str(rxcui),
            vocabulary_id='RxNorm',
        ).first()
        if existing:
            return existing

        # Cache the resolved concept
        return _create_rxnorm_concept(rxcui, canonical_name)

    except Exception as exc:
        logger.error('{"event": "rxnav_lookup_error", "drug": "%s", "error": "%s"}',
                     drug_source_value, exc)
        return None


def _rxnav_lookup(name: str):
    """Return (rxcui_str, canonical_name) for the active ingredient, or (None, None)."""
    url = f'{RXNAV_BASE}/drugs.json?name={urllib.parse.quote(name)}'
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())

    for group in data.get('drugGroup', {}).get('conceptGroup', []):
        if group.get('tty') == 'IN':
            props = group.get('conceptProperties', [])
            if props:
                return props[0]['rxcui'], props[0]['name']
    return None, None


def _create_rxnorm_concept(rxcui: str, canonical_name: str):
    """Create and return a minimal Concept row for an RxNav-resolved drug."""
    vocab, _ = Vocabulary.objects.get_or_create(
        vocabulary_id='RxNorm',
        defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
    )
    domain, _ = Domain.objects.get_or_create(
        domain_id='Drug',
        defaults={'domain_name': 'Drug', 'domain_concept_id': 13},
    )
    cc, _ = ConceptClass.objects.get_or_create(
        concept_class_id='Ingredient',
        defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
    )
    max_id = Concept.objects.order_by('-concept_id').values_list('concept_id', flat=True).first() or 2_000_000_000
    return Concept.objects.create(
        concept_id=max_id + 1,
        concept_name=canonical_name[:255],
        vocabulary=vocab,
        domain=domain,
        concept_class=cc,
        concept_code=str(rxcui)[:50],
        standard_concept='S',
        valid_start_date=date(1970, 1, 1),
        valid_end_date=date(2099, 12, 31),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.RxNavServiceTest --no-input 2>&1 | tail -5
```
Expected: `Ran 5 tests ... OK`

- [ ] **Step 5: Commit**

```bash
git add omop_core/services/rxnav_service.py patient_portal/tests.py
git commit -m "feat: add rxnav_service — resolve unknown drug names via RxNav API with local caching"
```

---

## Task 4: Wire rxnav_service into FHIR upload

**Files:**
- Modify: `patient_portal/api/views.py`
- Modify: `patient_portal/tests.py`

**Context:** In `views.py`, the FHIR upload processes `MedicationStatement` resources at around line 1362. When a named regimen (e.g., "Velcade") can't be matched to a local `Concept`, the code currently falls back to any Drug domain concept. We replace that fallback with a `resolve_drug()` call first.

- [ ] **Step 1: Write the failing test**

Add this class to `patient_portal/tests.py`:

```python
class FhirRxNavIntegrationTest(_SmartBase):
    """FHIR upload for a drug unknown in local vocab → RxNav called → concept resolved."""

    def _fhir_bundle_with_drug(self, drug_name):
        return {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': [
                {'resource': {
                    'resourceType': 'Patient',
                    'id': 'rxnav-test-pt-1',
                    'name': [{'family': 'RxNavTest', 'given': ['Patient']}],
                    'gender': 'female',
                    'birthDate': '1970-01-01',
                }},
                {'resource': {
                    'resourceType': 'MedicationStatement',
                    'id': 'rxnav-med-1',
                    'status': 'completed',
                    'subject': {'reference': 'Patient/rxnav-test-pt-1'},
                    'medicationCodeableConcept': {'text': drug_name},
                    'effectivePeriod': {'start': '2023-01-15', 'end': '2023-07-01'},
                    'extension': [
                        {'url': 'http://ctomop.io/fhir/StructureDefinition/therapy-line',
                         'valueInteger': 1},
                    ],
                }},
            ],
        }

    def test_fhir_upload_uses_rxnav_for_unknown_drug(self):
        """FHIR bundle with unknown drug name → RxNav resolves it → DrugExposure concept set."""
        from unittest.mock import patch
        from omop_core.models import DrugExposure

        with patch(
            'omop_core.services.rxnav_service._rxnav_lookup',
            return_value=('1421', 'bortezomib'),
        ):
            response = self.write_client.post(
                '/api/fhir/upload/',
                self._fhir_bundle_with_drug('Velcade'),
                format='json',
            )

        self.assertIn(response.status_code, [200, 201])
        de = DrugExposure.objects.filter(drug_source_value='Velcade').first()
        self.assertIsNotNone(de, 'DrugExposure for Velcade not created')
        self.assertNotEqual(
            de.drug_concept_id, 0,
            'drug_concept_id should be set via RxNav; got 0',
        )

    def test_fhir_upload_unknown_drug_rxnav_fails_gracefully(self):
        """RxNav returns nothing → FHIR upload still succeeds, uses fallback concept."""
        from unittest.mock import patch
        from omop_core.models import DrugExposure

        with patch(
            'omop_core.services.rxnav_service._rxnav_lookup',
            return_value=(None, None),
        ):
            response = self.write_client.post(
                '/api/fhir/upload/',
                self._fhir_bundle_with_drug('completely-unknown-drug-xyz'),
                format='json',
            )

        self.assertIn(response.status_code, [200, 201])
```

- [ ] **Step 2: Run to verify it fails**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test patient_portal.tests.FhirRxNavIntegrationTest --no-input 2>&1 | tail -5
```
Expected: test_fhir_upload_uses_rxnav_for_unknown_drug fails (drug_concept_id is 0)

- [ ] **Step 3: Add the import to views.py**

At the top of `patient_portal/api/views.py`, add this import alongside the other service imports:

```python
from omop_core.services.rxnav_service import resolve_drug as _rxnav_resolve_drug
```

- [ ] **Step 4: Replace the fallback concept lookup in views.py**

Find this block (around line 1362–1370 in `views.py`):

```python
                            regimen_concept = Concept.objects.filter(
                                concept_name__icontains=lot_data.get('regimen', ''),
                                domain__domain_id='Drug',
                            ).first() if lot_data.get('regimen') else None
                            # Fall back to any Drug domain concept when named one not found
                            if regimen_concept is None:
                                regimen_concept = Concept.objects.filter(
                                    domain__domain_id='Drug'
                                ).first()
```

Replace it with:

```python
                            regimen_name = lot_data.get('regimen', '')
                            regimen_concept = Concept.objects.filter(
                                concept_name__icontains=regimen_name,
                                domain__domain_id='Drug',
                            ).first() if regimen_name else None
                            # Try RxNav for drugs not in local vocabulary
                            if regimen_concept is None and regimen_name:
                                regimen_concept = _rxnav_resolve_drug(regimen_name)
                            # Final fallback to any Drug domain concept
                            if regimen_concept is None:
                                regimen_concept = Concept.objects.filter(
                                    domain__domain_id='Drug'
                                ).first()
```

- [ ] **Step 5: Run all four test classes**

```bash
DATABASE_URL="postgresql://ctomop_dev_user:IehVp8TGNcelOymGcjtfL6Up6W63DOf2@dpg-d7pqr35ckfvc73bm0lc0-a.oregon-postgres.render.com/ctomop_dev" \
  .venv/bin/python manage.py test \
    patient_portal.tests.VocabularyRelationshipModelTest \
    patient_portal.tests.AthenaVocabularyLoadTest \
    patient_portal.tests.RxNavServiceTest \
    patient_portal.tests.FhirRxNavIntegrationTest \
    --no-input 2>&1 | tail -10
```
Expected: `Ran 17 tests ... OK`

- [ ] **Step 6: Commit and push**

```bash
git add patient_portal/api/views.py patient_portal/tests.py
git commit -m "feat: wire rxnav_service into FHIR upload — resolve unknown drug names via RxNav API"
git push origin dev
```
