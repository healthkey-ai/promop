# Athena Vocabulary Loading + Full ARTEMIS LOT Inference — Design Spec

**Date:** 2026-05-17
**Status:** As-built design
**Related issues:** #67 (LOT inference), #31 (OMOP write-through)
**Depends on:** Athena account + TSV download (manual prerequisite)

---

## Problem

The current LOT inference service (`lot_inference_service.py`) uses a hardcoded `DRUG_SUBTYPE_MAP` keyed on lowercased drug source values. This breaks in three real-world scenarios:

1. **Brand names** — EHRs export "Velcade" not "bortezomib"; our lookup misses it entirely, falling back to alphabetic regimen naming.
2. **Novel agents** — teclistamab, elranatamab, talquetamab are not in the map; they default to `mixed` subtype and are excluded from myeloma-specific LOT logic.
3. **Map maintenance** — every new approved drug requires a manual code change.

The fix is to replace the hardcoded map with OHDSI-standard vocabulary: HemOnc for drug class hierarchy, RxNorm for brand→generic resolution, and the RxNav API as a real-time supplement for drugs not yet in our local vocabulary.

---

## Architecture

```
Athena TSV files (manual download)
    │
    ▼
manage.py load_athena_vocabularies
    │  Filters to: HemOnc + RxNorm oncology subset + ATC class L
    │  Bulk-loads via PostgreSQL COPY
    ▼
omop_core tables:
  Concept (extended)
  ConceptRelationship (new)
  ConceptAncestor (new)
  Relationship (new)
    │
    └──► lot_inference_service.py
            │  Drug class lookup via HemOnc concept_ancestor
            │  Brand→generic via ConceptRelationship "Maps to"
            ▼
         Episode + EpisodeEvent records
         (HealthTree phase rules preserved)

FHIR upload → drug_concept_id = 0?
    │
    ▼
rxnav_service.resolve_drug(source_value)
    │  RxNav API → RXCUI → active ingredient
    │  Cache result in Concept table
    ▼
DrugExposure.drug_concept_id updated
```

---

## Vocabulary Scope

Only vocabularies needed for ARTEMIS LOT inference are loaded. Full SNOMED, LOINC, and ICD-10 are out of scope.

| Vocabulary | Filter | Rationale |
|---|---|---|
| HemOnc | All concepts | Drug class hierarchy for ARTEMIS classification |
| RxNorm | `concept_class_id IN ('Ingredient', 'Clinical Drug', 'Branded Drug', 'Clinical Drug Comp')` | Brand→generic mapping; drug concept IDs in DrugExposure |
| RxNorm Extension | Same class filter | Covers newer agents not yet in core RxNorm |
| ATC | Class L (`concept_code LIKE 'L%'`) | Antineoplastic classification cross-reference |
| ConceptRelationship | Both concept_ids in filtered set | HemOnc↔RxNorm "Maps to", "Is a" edges |
| ConceptAncestor | HemOnc only | Drug class hierarchy traversal |
| Relationship | All (~700 rows) | Metadata for relationship types |

Estimated loaded rows: ~30K concepts, ~100K concept_relationships, ~50K concept_ancestors.

Athena TSV files are **not committed to git**. The download is a manual prerequisite (free Athena account at athena.ohdsi.org, vocabulary selection: HemOnc, RxNorm, RxNorm Extension, ATC, Concept Relationship, Concept Ancestor).

---

## New Models (`omop_core/models.py`)

### Relationship

```python
class Relationship(models.Model):
    relationship_id = models.CharField(max_length=20, primary_key=True)
    relationship_name = models.CharField(max_length=255)
    is_hierarchical = models.CharField(max_length=1)
    defines_ancestry = models.CharField(max_length=1)
    reverse_relationship_id = models.CharField(max_length=20)
    relationship_concept = models.ForeignKey(
        'Concept', on_delete=models.DO_NOTHING, db_column='relationship_concept_id'
    )

    class Meta:
        db_table = 'relationship'
```

### ConceptRelationship

```python
class ConceptRelationship(models.Model):
    concept_1 = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='relationships_as_source', db_column='concept_id_1'
    )
    concept_2 = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='relationships_as_target', db_column='concept_id_2'
    )
    relationship = models.ForeignKey(
        Relationship, on_delete=models.DO_NOTHING, db_column='relationship_id'
    )
    valid_start_date = models.DateField()
    valid_end_date = models.DateField()
    invalid_reason = models.CharField(max_length=1, null=True, blank=True)

    class Meta:
        db_table = 'concept_relationship'
        unique_together = [('concept_1', 'concept_2', 'relationship')]
```

### ConceptAncestor

```python
class ConceptAncestor(models.Model):
    ancestor_concept = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='descendants', db_column='ancestor_concept_id'
    )
    descendant_concept = models.ForeignKey(
        Concept, on_delete=models.DO_NOTHING,
        related_name='ancestors', db_column='descendant_concept_id'
    )
    min_levels_of_separation = models.IntegerField()
    max_levels_of_separation = models.IntegerField()

    class Meta:
        db_table = 'concept_ancestor'
        unique_together = [('ancestor_concept', 'descendant_concept')]
```

---

## Management Command: `load_athena_vocabularies`

```
manage.py load_athena_vocabularies --path /path/to/athena/ [--replace] [--dry-run]
```

**Arguments:**
- `--path` — directory containing Athena TSV files (`CONCEPT.csv`, `CONCEPT_RELATIONSHIP.csv`, `CONCEPT_ANCESTOR.csv`, `RELATIONSHIP.csv`, `VOCABULARY.csv`)
- `--replace` — drop and reload vocabulary-specific rows before inserting (safe for re-runs after a new Athena release); does not touch clinical data tables
- `--dry-run` — parse and count rows that would be loaded, print summary, write nothing

**Load sequence:**
1. Load `RELATIONSHIP.csv` → `Relationship` table (small, load fully)
2. Load `CONCEPT.csv` filtered to vocabulary scope → `Concept` table (upsert)
3. Load `CONCEPT_RELATIONSHIP.csv` filtered to rows where both concept_ids are in loaded set → `ConceptRelationship`
4. Load `CONCEPT_ANCESTOR.csv` filtered to HemOnc concepts only → `ConceptAncestor`

**Performance:** Uses `psycopg` `COPY` via a `StringIO` buffer for bulk insert. Filtered rows are streamed one pass through each TSV — no full file load into memory.

**Output:** Prints row counts per table and elapsed time. Errors on missing files or schema mismatches.

**Idempotency:** `ON CONFLICT DO NOTHING` on all inserts; `--replace` first deletes by vocabulary_id before reinserting.

---

## RxNav Service (`omop_core/services/rxnav_service.py`)

Entry point: `resolve_drug(drug_source_value: str) -> Concept | None`

**Algorithm:**
1. Check `Concept` table for `concept_code = normalized(drug_source_value)` and `vocabulary_id IN ('RxNorm', 'RxNorm Extension')` — return immediately if found (already resolved)
2. Call `GET https://rxnav.nlm.nih.gov/REST/drugs.json?name={urllib.parse.quote(drug_source_value)}` — find candidate RXCUIs
3. If results: take the first `IN` (ingredient) type entry; call `GET /rxcui/{rxcui}/properties.json` to get canonical name
4. Check again if that RXCUI's `concept_code` is in local `Concept` — return if found
5. If not found locally: create minimal `Concept` row (`vocabulary_id='RxNorm'`, `standard_concept='S'`, `concept_code=rxcui`, `concept_name=canonical_name`, `domain_id='Drug'`, `concept_class_id='Ingredient'`) and return it
6. Return `None` if RxNav returns no results or HTTP error (never raises)

**Where it's called:** In the FHIR upload path (`upload_fhir_bundle` in `views.py`), after `DrugExposure` creation, when `drug_concept_id = 0`. The resolved concept's `concept_id` is written back to `DrugExposure.drug_concept_id`.

**Caching:** Concept rows written to the DB are permanent. The same drug source value will match on step 1 in all future uploads — RxNav is called at most once per unique drug name.

**Tests:** RxNav HTTP calls are mocked via `unittest.mock.patch`. Tests verify: known drug resolves, unknown drug returns None, resolved concept is persisted, duplicate calls return cached row.

---

## LOT Inference Updates (`lot_inference_service.py`)

The algorithm structure (6 phases) is unchanged. Only drug classification changes.

### Replace DRUG_SUBTYPE_MAP with HemOnc vocabulary lookup

Current code classifies a drug by looking up `drug_key` in `DRUG_SUBTYPE_MAP`. Replacement:

```python
def _classify_drug(drug_concept_id: int, drug_source_value: str) -> str:
    """Return drug subtype: myeloma / cart / steroid / mixed.

    Two-step traversal:
      Step 1 — RxNorm concept → HemOnc drug concept via ConceptRelationship "Maps to"
      Step 2 — HemOnc drug concept → HemOnc ancestor classes via ConceptAncestor
    """
    if drug_concept_id:
        # Step 1: find HemOnc concept(s) this RxNorm drug maps to
        hemonc_ids = list(
            ConceptRelationship.objects.filter(
                concept_1_id=drug_concept_id,
                relationship_id='Maps to',
                concept_2__vocabulary_id='HemOnc',
            ).values_list('concept_2_id', flat=True)
        )
        if hemonc_ids:
            # Step 2: walk HemOnc ancestor hierarchy
            ancestor_names = set(
                ConceptAncestor.objects.filter(
                    descendant_concept_id__in=hemonc_ids,
                ).values_list('ancestor_concept__concept_name', flat=True)
            )
            if ancestor_names & HEMONC_CART_CLASSES:
                return 'cart'
            if ancestor_names & HEMONC_MYELOMA_CLASSES:
                return 'myeloma'
            if ancestor_names & HEMONC_STEROID_CLASSES:
                return 'steroid'
    # Fallback: hardcoded map (covers RxNav-cached concepts not yet in HemOnc)
    return DRUG_SUBTYPE_MAP.get(drug_source_value.lower().strip(), 'mixed')
```

HemOnc class name sets (defined in `lot_regimens.py`):

```python
HEMONC_MYELOMA_CLASSES = frozenset({
    'Proteasome inhibitor', 'Immunomodulatory agent', 'Anti-CD38 monoclonal antibody',
    'Anti-SLAMF7 monoclonal antibody', 'Nuclear export inhibitor',
    'Alkylating agent',  # melphalan in myeloma context
    'BCL-2 inhibitor',   # venetoclax t(11;14)
    'BCMA-targeted agent',
})
HEMONC_CART_CLASSES = frozenset({'CAR T-cell therapy'})
HEMONC_STEROID_CLASSES = frozenset({'Corticosteroid', 'Supportive care agent'})
```

### Brand→generic resolution during era construction

In Phase 1 (build drug eras), group by `drug_concept_id` first (reliable when non-zero). For rows where `drug_concept_id = 0`, fall back to `drug_source_value` grouping. The RxNav service is called at FHIR upload time (not during inference), so by inference time `drug_concept_id` should be populated for any drug that RxNav could resolve.

### HealthTree rules preserved unchanged

Transplant rule, tandem transplant exception, CAR-T rule, phase labels, bridging detection — all unchanged. These operate on `ProcedureOccurrence` SNOMED codes, not on drug vocabulary.

### Regimen naming

The `MYELOMA_REGIMEN_LOOKUP` frozenset matching in `lot_regimens.py` is preserved. With HemOnc-based resolution, drug names going into the frozenset are now canonical RxNorm ingredient names (not EHR source values), so lookup accuracy improves substantially.

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Modify | `omop_core/models.py` | Add `Relationship`, `ConceptRelationship`, `ConceptAncestor` |
| Create | `omop_core/migrations/XXXX_add_vocabulary_relationship_tables.py` | Schema migration |
| Create | `omop_core/management/commands/load_athena_vocabularies.py` | Athena TSV loader |
| Create | `omop_core/services/rxnav_service.py` | RxNav API + concept cache |
| Modify | `omop_core/services/lot_regimens.py` | Add HemOnc class name sets |
| Modify | `omop_core/services/lot_inference_service.py` | Replace DRUG_SUBTYPE_MAP with `_classify_drug()` |
| Modify | `patient_portal/api/views.py` | Call `rxnav_service.resolve_drug()` after DrugExposure creation |
| Modify | `patient_portal/tests.py` | Add `AthenaVocabularyTest`, `RxNavServiceTest`, `ArtemisLotTest` |

---

## Deployment

**First-time setup (dev and production):**
1. Download vocabularies from athena.ohdsi.org (free account; select HemOnc, RxNorm, RxNorm Extension, ATC, Concept, Concept Relationship, Concept Ancestor)
2. Run: `DATABASE_URL=... python manage.py load_athena_vocabularies --path /path/to/download/`
3. Verify: `python manage.py shell -c "from omop_core.models import Concept; print(Concept.objects.filter(vocabulary_id='HemOnc').count())"`

**Vocabulary refresh (quarterly Athena releases):**
- Download new TSVs, re-run with `--replace` flag
- No migration required; data-only update

**Render production:**
- Run management command once as a one-off Render job after first deploy
- Athena TSVs transferred to the Render host (e.g. via `scp`, Render shell, or a pre-upload to object storage) and deleted after load — they are not stored persistently on the app host

---

## Tests

New test classes in `patient_portal/tests.py`:

| Class | Tests |
|---|---|
| `AthenaVocabularyLoadTest` | Load from minimal fixture TSVs; verify counts; idempotency on re-run; `--dry-run` writes nothing; `--replace` clears and reloads |
| `RxNavServiceTest` | Known drug resolves to Concept; unknown drug returns None; resolved concept persisted; cached on second call (no API hit); HTTP error returns None |
| `DrugClassificationTest` | HemOnc ancestor lookup returns correct subtype; fallback to DRUG_SUBTYPE_MAP when concept_id=0; novel drug (not in HemOnc) returns `mixed` |
| `ArtemisLotTest` | Brand name drug (`Velcade`) classified as `myeloma` via RxNorm→HemOnc; regimen lookup produces `VRD` with RxNorm-resolved names; transplant rule still fires correctly with HemOnc classification |

Tests seed minimal vocabulary rows directly (no Athena TSV files required in CI). RxNav calls are mocked.

---

## Out of Scope

- Loading SNOMED, LOINC, ICD-10 vocabularies (separate future work)
- Automated Athena TSV download (requires Athena login; manual download is standard OHDSI practice)
- Full UMLS loading
- Observation period table (`observation_period`) and washout filtering — deferred to a future ARTEMIS compliance pass
- AI-augmented regimen extraction
