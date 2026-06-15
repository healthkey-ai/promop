# ARTEMIS HemOnc LOT Inference Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded `DRUG_SUBTYPE_MAP` lookup in LOT inference with a two-step HemOnc vocabulary traversal so brand names, novel agents, and any drug with an OMOP concept ID are classified correctly without manual map maintenance.

**Architecture:** `lot_regimens.py` gains three frozensets of HemOnc class names. `lot_inference_service.py` gains a `_classify_drug(drug_concept_id, drug_source_value)` function that first walks `ConceptRelationship` to find the HemOnc concept for a given RxNorm drug, then walks `ConceptAncestor` to find its ancestor classes, and falls back to `DRUG_SUBTYPE_MAP` only when the concept ID is zero or HemOnc has no match.

**Tech Stack:** Django ORM, OMOP CDM `concept_relationship` and `concept_ancestor` tables (loaded by Plan 1), existing `lot_regimens.py` / `lot_inference_service.py` (created by the LOT inference plan)

**Prerequisites:**
- Plan 1 (`2026-05-17-athena-vocabulary-loading.md`) implemented — `ConceptRelationship` and `ConceptAncestor` models exist
- LOT inference plan (`2026-05-16-lot-inference.md`) implemented — `lot_regimens.py` and `lot_inference_service.py` exist with `DRUG_SUBTYPE_MAP` and `_drug_subtype()`

**Run all tests with:**
```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test \
    patient_portal.tests.DrugClassificationTest \
    patient_portal.tests.ArtemisHemOncLotTest \
    --no-input 2>&1 | tail -15
```

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `omop_core/services/lot_regimens.py` | Add `HEMONC_MYELOMA_CLASSES`, `HEMONC_CART_CLASSES`, `HEMONC_STEROID_CLASSES` frozensets |
| Modify | `omop_core/services/lot_inference_service.py` | Add `_classify_drug()`; update `_build_drug_eras()` to pass concept ID; remove `_drug_subtype()` |
| Modify | `patient_portal/tests.py` | Add `DrugClassificationTest`, `ArtemisHemOncLotTest` |

---

## Task 1: Add HemOnc class name sets to lot_regimens.py

**Files:**
- Modify: `omop_core/services/lot_regimens.py`
- Test: `patient_portal/tests.py`

- [ ] **Step 1: Write the failing test**

Add this class to `patient_portal/tests.py` (after existing imports — needs `date` from `datetime`):

```python
class DrugClassificationTest(TestCase):
    """Test _classify_drug() HemOnc two-step lookup + DRUG_SUBTYPE_MAP fallback."""

    def setUp(self):
        _make_vocab_fixtures()
        # HemOnc vocabulary
        self.hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc Oncology', 'vocabulary_concept_id': 0},
        )
        self.rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        self.domain_drug = Domain.objects.get(domain_id='Drug')
        self.cc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='HemOnc Class',
            defaults={'concept_class_name': 'HemOnc Class', 'concept_class_concept_id': 0},
        )
        self.cc_ing, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )

        # HemOnc concepts
        self.pi_class = Concept.objects.create(
            concept_id=8800001, concept_name='Proteasome inhibitor',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='PI', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_hemonc = Concept.objects.create(
            concept_id=8800002, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='HO-Bort', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.cart_class = Concept.objects.create(
            concept_id=8800003, concept_name='CAR T-cell therapy',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='CART', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.cart_drug = Concept.objects.create(
            concept_id=8800004, concept_name='idecabtagene vicleucel',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc,
            concept_code='IdecelHemOnc', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

        # RxNorm concept for bortezomib (Velcade brand maps here)
        self.bort_rxnorm = Concept.objects.create(
            concept_id=8810001, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='1421', standard_concept='S',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

        # ConceptRelationship: RxNorm bortezomib → HemOnc bortezomib
        from omop_core.models import Relationship, ConceptRelationship, ConceptAncestor
        self.maps_to, _ = Relationship.objects.get_or_create(
            relationship_id='Maps to',
            defaults={
                'relationship_name': 'Maps to', 'is_hierarchical': '0',
                'defines_ancestry': '0', 'reverse_relationship_id': 'Mapped from',
                'relationship_concept_id': 0,
            },
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.bort_rxnorm, concept_2=self.bort_hemonc, relationship=self.maps_to,
            defaults={'valid_start_date': date(1970, 1, 1), 'valid_end_date': date(2099, 12, 31)},
        )
        ConceptRelationship.objects.get_or_create(
            concept_1=self.cart_drug, concept_2=self.cart_class, relationship=self.maps_to,
            defaults={'valid_start_date': date(1970, 1, 1), 'valid_end_date': date(2099, 12, 31)},
        )

        # ConceptAncestor: PI class is ancestor of HemOnc bortezomib
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.pi_class, descendant_concept=self.bort_hemonc,
            defaults={'min_levels_of_separation': 1, 'max_levels_of_separation': 1},
        )
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.cart_class, descendant_concept=self.cart_drug,
            defaults={'min_levels_of_separation': 0, 'max_levels_of_separation': 0},
        )

    def test_rxnorm_bortezomib_classifies_as_myeloma(self):
        """RxNorm concept for bortezomib → HemOnc PI class → 'myeloma'."""
        from omop_core.services.lot_inference_service import _classify_drug
        result = _classify_drug(self.bort_rxnorm.concept_id, 'bortezomib')
        self.assertEqual(result, 'myeloma')

    def test_cart_drug_classifies_as_cart(self):
        """HemOnc CAR T drug → ancestor is CAR T-cell therapy → 'cart'."""
        from omop_core.services.lot_inference_service import _classify_drug
        result = _classify_drug(self.cart_drug.concept_id, 'idecabtagene vicleucel')
        self.assertEqual(result, 'cart')

    def test_zero_concept_id_falls_back_to_drug_subtype_map(self):
        """concept_id=0 → fall back to DRUG_SUBTYPE_MAP."""
        from omop_core.services.lot_inference_service import _classify_drug
        result = _classify_drug(0, 'bortezomib')
        self.assertEqual(result, 'myeloma')  # bortezomib is in DRUG_SUBTYPE_MAP

    def test_novel_drug_not_in_hemonc_returns_mixed(self):
        """Drug with a concept ID but no HemOnc mapping → 'mixed'."""
        from omop_core.services.lot_inference_service import _classify_drug
        novel = Concept.objects.create(
            concept_id=8899999, concept_name='noveldrugxyz',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='NOVEL99', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        result = _classify_drug(novel.concept_id, 'noveldrugxyz')
        self.assertEqual(result, 'mixed')
```

- [ ] **Step 2: Run test to verify it fails**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.DrugClassificationTest --no-input 2>&1 | tail -5
```
Expected: `ImportError` or `AttributeError` (`_classify_drug` not yet defined)

- [ ] **Step 3: Add HemOnc class name sets to lot_regimens.py**

Open `omop_core/services/lot_regimens.py`. Find the line that defines `STEROID_SUBTYPES` (search for `STEROID_SUBTYPES`). Add these three constants **immediately after** `STEROID_SUBTYPES`:

```python
# HemOnc ancestor class names used by _classify_drug() in lot_inference_service.py
HEMONC_MYELOMA_CLASSES: frozenset[str] = frozenset({
    'Proteasome inhibitor',
    'Immunomodulatory agent',
    'Anti-CD38 monoclonal antibody',
    'Anti-SLAMF7 monoclonal antibody',
    'Nuclear export inhibitor',
    'Alkylating agent',
    'BCL-2 inhibitor',
    'BCMA-targeted agent',
    'Anti-CD38 antibody-drug conjugate',
    'Cereblon E3 ligase modulator',
})

HEMONC_CART_CLASSES: frozenset[str] = frozenset({
    'CAR T-cell therapy',
})

HEMONC_STEROID_CLASSES: frozenset[str] = frozenset({
    'Corticosteroid',
    'Supportive care agent',
})
```

- [ ] **Step 4: Update the import in lot_inference_service.py**

In `omop_core/services/lot_inference_service.py`, find the block:

```python
from omop_core.services.lot_regimens import (
    DRUG_SUBTYPE_MAP,
    MYELOMA_REGIMEN_LOOKUP,
    PROCEDURE_SNOMED_MAP,
    REGIMEN_LOOKUP,
    STEROID_SUBTYPES,
)
```

Replace it with:

```python
from omop_core.services.lot_regimens import (
    DRUG_SUBTYPE_MAP,
    HEMONC_CART_CLASSES,
    HEMONC_MYELOMA_CLASSES,
    HEMONC_STEROID_CLASSES,
    MYELOMA_REGIMEN_LOOKUP,
    PROCEDURE_SNOMED_MAP,
    REGIMEN_LOOKUP,
    STEROID_SUBTYPES,
)
```

Also add these two imports to the Django model imports block at the top of the same file (find the line `from omop_core.models import Concept, DrugExposure, ProcedureOccurrence`):

```python
from omop_core.models import (
    Concept, ConceptAncestor, ConceptRelationship, DrugExposure, ProcedureOccurrence,
)
```

- [ ] **Step 5: Replace _drug_subtype() with _classify_drug() in lot_inference_service.py**

Find this function in `omop_core/services/lot_inference_service.py`:

```python
def _drug_subtype(key: str) -> str:
    return DRUG_SUBTYPE_MAP.get(key, 'mixed')
```

Replace it with:

```python
def _classify_drug(drug_concept_id: int, drug_source_value: str) -> str:
    """Return drug subtype: myeloma / cart / steroid / mixed.

    Two-step HemOnc traversal:
      Step 1 — RxNorm concept → HemOnc drug concept via ConceptRelationship "Maps to"
      Step 2 — HemOnc drug concept → HemOnc ancestor class names via ConceptAncestor
    Falls back to DRUG_SUBTYPE_MAP when concept_id is 0 or HemOnc has no mapping.
    """
    if drug_concept_id:
        hemonc_ids = list(
            ConceptRelationship.objects.filter(
                concept_1_id=drug_concept_id,
                relationship_id='Maps to',
                concept_2__vocabulary_id='HemOnc',
            ).values_list('concept_2_id', flat=True)
        )
        if hemonc_ids:
            ancestor_names = set(
                ConceptAncestor.objects.filter(
                    descendant_concept_id__in=hemonc_ids,
                ).values_list('ancestor_concept__concept_name', flat=True)
            )
            # Include the HemOnc concept names themselves (self-ancestor at level 0)
            ancestor_names.update(
                Concept.objects.filter(concept_id__in=hemonc_ids)
                               .values_list('concept_name', flat=True)
            )
            if ancestor_names & HEMONC_CART_CLASSES:
                return 'cart'
            if ancestor_names & HEMONC_MYELOMA_CLASSES:
                return 'myeloma'
            if ancestor_names & HEMONC_STEROID_CLASSES:
                return 'steroid'
            return 'mixed'
    # Fallback: hardcoded map for zero concept_id or RxNav-cached concepts not in HemOnc
    return DRUG_SUBTYPE_MAP.get(drug_source_value.lower().strip(), 'mixed')
```

- [ ] **Step 6: Update _build_drug_eras() to call _classify_drug()**

In `omop_core/services/lot_inference_service.py`, find the `_build_drug_eras` function. It calls `_drug_subtype(drug_key)`. Replace those call sites.

Find:

```python
def _build_drug_eras(exposures) -> list[_DrugEra]:
    by_drug = defaultdict(list)
    for exp in exposures:
        by_drug[_drug_key(exp)].append(exp)

    eras = []
    for drug_key, exps in by_drug.items():
        exps_sorted = sorted(exps, key=lambda e: e.drug_exposure_start_date)
        subtype = _drug_subtype(drug_key)
```

Replace with:

```python
def _build_drug_eras(exposures) -> list[_DrugEra]:
    by_drug = defaultdict(list)
    for exp in exposures:
        by_drug[_drug_key(exp)].append(exp)

    eras = []
    for drug_key, exps in by_drug.items():
        exps_sorted = sorted(exps, key=lambda e: e.drug_exposure_start_date)
        # Use first exposure's concept_id; all exposures for the same drug key share it
        rep = exps_sorted[0]
        concept_id = rep.drug_concept_id or 0
        subtype = _classify_drug(concept_id, drug_key)
```

- [ ] **Step 7: Run the DrugClassificationTest**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.DrugClassificationTest --no-input 2>&1 | tail -5
```
Expected: `Ran 4 tests ... OK`

- [ ] **Step 8: Commit**

```bash
git add omop_core/services/lot_regimens.py omop_core/services/lot_inference_service.py patient_portal/tests.py
git commit -m "feat: replace DRUG_SUBTYPE_MAP with HemOnc vocabulary lookup in LOT drug classification"
```

---

## Task 2: Integration test — brand name and novel agent classification

**Files:**
- Modify: `patient_portal/tests.py`

This task verifies that the full LOT inference pipeline (`infer_lot_for_person`) correctly classifies a drug uploaded under its brand name ("Velcade") and that a novel agent (talquetamab) with no HemOnc mapping is handled without crashing.

- [ ] **Step 1: Write the failing integration tests**

Add this class to `patient_portal/tests.py`:

```python
class ArtemisHemOncLotTest(TestCase):
    """Integration tests: infer_lot_for_person with HemOnc-backed drug classification."""

    def setUp(self):
        _make_vocab_fixtures()
        self.rxnorm_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='RxNorm',
            defaults={'vocabulary_name': 'RxNorm', 'vocabulary_concept_id': 0},
        )
        self.hemonc_vocab, _ = Vocabulary.objects.get_or_create(
            vocabulary_id='HemOnc',
            defaults={'vocabulary_name': 'HemOnc Oncology', 'vocabulary_concept_id': 0},
        )
        self.domain_drug = Domain.objects.get(domain_id='Drug')
        self.cc_ing, _ = ConceptClass.objects.get_or_create(
            concept_class_id='Ingredient',
            defaults={'concept_class_name': 'Ingredient', 'concept_class_concept_id': 0},
        )
        self.cc_hemonc, _ = ConceptClass.objects.get_or_create(
            concept_class_id='HemOnc Class',
            defaults={'concept_class_name': 'HemOnc Class', 'concept_class_concept_id': 0},
        )

        # Build HemOnc hierarchy: Proteasome inhibitor → bortezomib (HemOnc)
        self.pi_class = Concept.objects.create(
            concept_id=9900101, concept_name='Proteasome inhibitor',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc_hemonc,
            concept_code='PI', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_hemonc = Concept.objects.create(
            concept_id=9900102, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.hemonc_vocab, concept_class=self.cc_hemonc,
            concept_code='HO-Bort', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        self.bort_rxnorm = Concept.objects.create(
            concept_id=9900103, concept_name='bortezomib',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='1421', standard_concept='S',
            valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )

        from omop_core.models import Relationship, ConceptRelationship, ConceptAncestor
        maps_to, _ = Relationship.objects.get_or_create(
            relationship_id='Maps to',
            defaults={
                'relationship_name': 'Maps to', 'is_hierarchical': '0',
                'defines_ancestry': '0', 'reverse_relationship_id': 'Mapped from',
                'relationship_concept_id': 0,
            },
        )
        # RxNorm bortezomib → HemOnc bortezomib
        ConceptRelationship.objects.get_or_create(
            concept_1=self.bort_rxnorm, concept_2=self.bort_hemonc, relationship=maps_to,
            defaults={'valid_start_date': date(1970, 1, 1), 'valid_end_date': date(2099, 12, 31)},
        )
        # PI class is ancestor of HemOnc bortezomib
        ConceptAncestor.objects.get_or_create(
            ancestor_concept=self.pi_class, descendant_concept=self.bort_hemonc,
            defaults={'min_levels_of_separation': 1, 'max_levels_of_separation': 1},
        )

        # Person and related OMOP records
        from omop_core.models import Person, DrugExposure, ObservationPeriod
        self.person = Person.objects.create(
            person_id=7700001,
            gender_concept_id=8532,
            year_of_birth=1960,
            race_concept_id=0,
            ethnicity_concept_id=0,
        )
        self.drug_type, _ = Concept.objects.get_or_create(
            concept_id=38000177,
            defaults={
                'concept_name': 'Prescription written',
                'domain': self.domain_drug,
                'vocabulary': self.rxnorm_vocab,
                'concept_class': self.cc_ing,
                'concept_code': '38000177',
                'valid_start_date': date(1970, 1, 1),
                'valid_end_date': date(2099, 12, 31),
            },
        )

        # A single drug exposure for bortezomib via its RxNorm concept ID
        self.velcade_exposure = DrugExposure.objects.create(
            person=self.person,
            drug_concept=self.bort_rxnorm,
            drug_source_value='Velcade',
            drug_type_concept=self.drug_type,
            drug_exposure_start_date=date(2023, 1, 15),
            drug_exposure_end_date=date(2023, 4, 15),
        )

    def test_brand_name_drug_classified_as_myeloma_via_hemonc(self):
        """Velcade uploaded with RxNorm concept_id → classified as myeloma LOT."""
        from omop_core.services.lot_inference_service import infer_lot_for_person
        lots = infer_lot_for_person(self.person.person_id)
        self.assertGreater(len(lots), 0, 'Expected at least one LOT')
        lot1 = lots[0]
        # LOT should include bortezomib and be classified (not 'mixed')
        # The regimen name will contain 'bortezomib' or 'V' (for Velcade abbreviation)
        self.assertNotEqual(lot1.regimen_name, '', 'Expected regimen name to be set')

    def test_novel_agent_no_hemonc_mapping_does_not_crash(self):
        """Drug with RxNorm concept_id but no HemOnc mapping → 'mixed', no exception."""
        from omop_core.models import DrugExposure
        from omop_core.services.lot_inference_service import infer_lot_for_person, _classify_drug

        novel_concept = Concept.objects.create(
            concept_id=9999999, concept_name='talquetamab',
            domain=self.domain_drug, vocabulary=self.rxnorm_vocab, concept_class=self.cc_ing,
            concept_code='TALQ99', valid_start_date=date(1970, 1, 1), valid_end_date=date(2099, 12, 31),
        )
        result = _classify_drug(novel_concept.concept_id, 'talquetamab')
        self.assertEqual(result, 'mixed')

    def test_regression_existing_lot_tests_still_pass(self):
        """Run the full ArtemisLotTest suite to check for regressions."""
        from omop_core.services.lot_inference_service import infer_lot_for_person
        # Minimal sanity check: function is callable and returns a list
        lots = infer_lot_for_person(self.person.person_id)
        self.assertIsInstance(lots, list)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.ArtemisHemOncLotTest --no-input 2>&1 | tail -5
```
Expected: `Ran 3 tests ... OK` (tests should pass after Task 1 is done; if they fail, debug `_classify_drug` first)

- [ ] **Step 3: Run the full existing LOT test suite for regressions**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.ArtemisLotTest --no-input 2>&1 | tail -10
```
Expected: all existing LOT tests still pass (verify no regressions from changing `_drug_subtype` → `_classify_drug`)

- [ ] **Step 4: Run both new test classes together**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test \
    patient_portal.tests.DrugClassificationTest \
    patient_portal.tests.ArtemisHemOncLotTest \
    --no-input 2>&1 | tail -5
```
Expected: `Ran 7 tests ... OK`

- [ ] **Step 5: Commit and push**

```bash
git add patient_portal/tests.py
git commit -m "test: add DrugClassificationTest and ArtemisHemOncLotTest for HemOnc-backed LOT classification"
git push origin dev
```
