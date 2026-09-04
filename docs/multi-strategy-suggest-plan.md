# Multi-Strategy Suggest for Code Mappings — Implementation Plan

**Status:** Implementation complete, awaiting test suite verification + PR
**Branch:** `feat/multi-strategy-suggest`
**Tracking issue:** (not yet filed)

---

## Overview

The Code Mapping page's Suggest button currently uses two stages: **lexical retrieval** (GIN trigram on `concept` + `concept_synonym`) then **Claude re-ranking**. This plan adds two new retrieval tiers ahead of lexical — UMLS lookup and vector similarity — creating a three-tier waterfall with user-controllable checkboxes.

## Architecture: Three-Tier Waterfall

```
For each unmapped source code:

  [x] UMLS    ──→  CUI bridge lookup  ──→  found standard concept?  ──→ YES → done
                                                    │ NO
  [x] Vectors ──→  embed & cosine search ──→  candidates  ──→ rank_candidates()
                                                    │ no good match
  [x] Lexical ──→  trigram similarity    ──→  candidates  ──→ rank_candidates()
```

Each tier is opt-in via checkbox. The waterfall exits early: if UMLS finds a
definitive standard-concept match, vectors and lexical are skipped for that code.
Vectors and lexical both feed candidates into the existing `rank_candidates()`
(Claude re-ranker).

---

## Task Checklist

### 1. Verify pgvector on Render staging
- [x] Run `CREATE EXTENSION IF NOT EXISTS vector` on staging DB
- [x] If unavailable, plan fallback to in-memory numpy cosine (no index, slower)
- [x] Document result here

**Result:** pgvector v0.8.1 already installed on staging. No fallback needed.

### 2. Implement UMLS tier — `umls_candidates()`
- [x] Verify actual `root_source` values in staging UMLS data (195 distinct values confirmed)
- [x] Add `VOCAB_TO_UMLS_ROOT` constant to `mapping_suggestions.py`
- [x] Implement `umls_candidates(source_code, source_vocabulary_id, domain_id)` in `omop_core/services/mapping_suggestions.py`
- [x] Unit tests for UMLS lookup (6 tests in TestUmlsCandidates)

**How it works:**
1. Map `source_vocabulary_id` → UMLS `root_source` (e.g. `SNOMED` → `SNOMEDCT_US`)
2. Query `UmlsSourceCode` to find CUI: `WHERE root_source = ? AND code = ?`
3. Query all other `UmlsSourceCode` rows sharing that CUI
4. For each sibling code, look up `Concept` in OMOP: `WHERE vocabulary_id = ? AND concept_code = ? AND standard_concept = 'S'`
5. Single standard concept → high-confidence match (skip ranker)
6. Multiple standard concepts → return all as candidates for Claude to rank

**Preliminary root-source mapping** (verify against staging data):
```python
VOCAB_TO_UMLS_ROOT = {
    'SNOMED': 'SNOMEDCT_US',
    'ICD10CM': 'ICD10CM',
    'ICD10PCS': 'ICD10PCS',
    'LOINC': 'LNC',
    'RxNorm': 'RXNORM',
    'CPT4': 'CPT',
    'HCPCS': 'HCPCS',
    'NDC': 'NDC',
    'CVX': 'CVX',
}
```

### 3. Implement Vector tier — model, migration, query function
- [x] Add `pgvector` and `sentence-transformers` to `requirements.txt`
- [x] Add `ConceptEmbedding` model to `omop_core/models.py`
- [x] Create migration 0204 (conditional: skips on systems without pgvector)
- [x] Implement `vector_candidates(source_value, domain_id, limit)` in `mapping_suggestions.py`

**Model:**
```python
class ConceptEmbedding(models.Model):
    concept = models.OneToOneField(Concept, primary_key=True, on_delete=models.CASCADE)
    embedding = VectorField(dimensions=384)

    class Meta:
        db_table = 'concept_embedding'
```

**Query:** Uses pgvector `<=>` cosine distance operator, filtered by domain + standard_concept.

### 4. Create `build_concept_embeddings` management command
- [x] Create `omop_core/management/commands/build_concept_embeddings.py`
- [x] Loads `BAAI/bge-small-en-v1.5` (384 dimensions)
- [x] Embeds `concept_name` in batches of 512
- [x] Bulk upserts into `concept_embedding`
- [x] Flags: `--batch-size`, `--vocabulary-id`, `--force`
- [ ] Run on staging to populate embeddings

### 5. Implement waterfall orchestration in `suggest_mappings()`
- [x] Add `strategies: list[str]` parameter (default: `["umls", "vectors", "lexical"]`)
- [x] For each unmapped source value, run tiers in order, skipping unchecked ones
- [x] UMLS single-match → write mapping, skip other tiers
- [x] Multi-match or vector/lexical → feed to `rank_candidates()`
- [x] Track `strategy_used` per result

### 6. Update `code_mapping_suggest()` API endpoint
- [x] Accept `strategies` list in request body
- [x] Validate strategy names
- [x] Pass to `suggest_mappings()`
- [x] Include `strategy_used` and `umls_cui` in response + `strategy_counts` summary

**Request addition:**
```json
{
  "strategies": ["umls", "vectors", "lexical"]
}
```

**Response addition per result:**
```json
{
  "strategy_used": "umls",
  "umls_cui": "C0011860"
}
```

### 7. Add strategy checkboxes to frontend
- [x] Add state: `useState({umls: true, vectors: true, lexical: true})`
- [x] Add three checkboxes below min-occurrences input
- [x] Pass selected strategies to API call
- [x] Update banner to show strategy breakdown

### 8. Write tests
- [x] UMLS lookup with known equivalency (6 tests)
- [x] Vector candidates graceful degradation (3 tests)
- [x] Vocab-to-UMLS mapping consistency (2 tests)
- [x] API endpoint strategy parameter validation (4 tests)
- [ ] Integration test — full waterfall with each strategy combination
- [ ] Integration test — FHIR upload + suggest end-to-end

**Test file:** `tests/test_suggest_strategies.py`

**Run:**
```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python -m pytest tests/test_suggest_strategies.py -v
```

---

## Files to Create or Modify

| File | Action | What |
|---|---|---|
| `omop_core/services/mapping_suggestions.py` | Modify | Add `umls_candidates()`, `vector_candidates()`, waterfall + strategies |
| `omop_core/models.py` | Modify | Add `ConceptEmbedding` model |
| `omop_core/migrations/XXXX_add_concept_embedding.py` | Create (makemigrations) | pgvector extension + table |
| `omop_core/management/commands/build_concept_embeddings.py` | Create | Embedding generation command |
| `patient_portal/api/views.py` | Modify | Add `strategies` param to `code_mapping_suggest()` |
| `frontend/src/components/CodeMappings/CodeMappingPage.tsx` | Modify | Strategy checkboxes |
| `requirements.txt` | Modify | Add `pgvector`, `sentence-transformers` |
| `tests/test_suggest_strategies.py` | Create | Strategy tests |

---

## Dependency Order

```
Task 1: Verify pgvector on staging
 ├── Task 2: UMLS tier
 │    └── Task 5: Waterfall orchestration ──┐
 ├── Task 3: Vector tier (model + migration)┘
 │    └── Task 4: build_concept_embeddings command
 │
Task 5: Waterfall orchestration
 ├── Task 6: API endpoint
 │    └── Task 7: Frontend checkboxes
 └── Task 8: Tests
```

---

## Key Existing Code References

| What | Where |
|---|---|
| Current suggest orchestrator | `omop_core/services/mapping_suggestions.py` → `suggest_mappings()` |
| Lexical candidates | same file → `lexical_candidates()` |
| Claude re-ranker | same file → `rank_candidates()` |
| API view | `patient_portal/api/views.py` → `code_mapping_suggest()` |
| Frontend | `frontend/src/components/CodeMappings/CodeMappingPage.tsx` → `runSuggest()` |
| UmlsSourceCode model | `omop_core/models.py` line ~4181 |
| Concept model | `omop_core/models.py` line ~570 |

---

## Verification Checklist

- [ ] UMLS: Known ICD10CM→SNOMED equivalency returns correct standard concept
- [ ] Vectors: `vector_candidates("high blood pressure", "Condition")` returns hypertension concepts
- [ ] Waterfall: UMLS-only → only UMLS matches; all three → UMLS skips other tiers
- [ ] Frontend: Checkboxes render, toggle, pass to API; banner shows strategy breakdown
- [ ] All tests pass
