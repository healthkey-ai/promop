# Concept Mapping: LOINC, SNOMED, and HemOnc in PRomop

PRomop ingests clinical data as FHIR R4 Bundles and stores it in OMOP CDM v5.4 tables. This
document explains how clinical codes from FHIR resources — LOINC observation codes, SNOMED
condition codes, and HemOnc/RxNorm drug codes — are resolved to OMOP Concept IDs using the
OHDSI Athena vocabulary tables, and how those concepts are later used to derive the PatientRecord
projection.

---

## OMOP Vocabulary Tables

PRomop relies on three Athena vocabulary tables loaded into PostgreSQL:

| Table | Purpose |
|---|---|
| `concept` | Canonical registry of all clinical concepts across vocabularies |
| `concept_relationship` | Maps concepts across vocabularies (e.g. RxNorm drug → HemOnc regimen) |
| `concept_ancestor` | Hierarchical ancestry (e.g. a specific HemOnc drug → its drug class) |

These tables are populated by downloading vocabulary files from [OHDSI Athena](https://athena.ohdsi.org)
and loading them with the `load_athena_vocabularies` management command. See
[SYNTHETIC_PATIENT_GENERATION.md](../SYNTHETIC_PATIENT_GENERATION.md) for instructions.

### Key concept fields

```
concept_id       integer    Primary key — referenced everywhere as an FK
concept_code     varchar    The code in the source vocabulary (e.g. '718-7' for LOINC)
vocabulary_id    varchar    The vocabulary name: 'LOINC', 'SNOMED', 'RxNorm', 'HemOnc', ...
concept_name     varchar    Human-readable label (GIN-indexed for fast text search)
domain_id        varchar    Clinical domain: 'Measurement', 'Condition', 'Drug', ...
standard_concept char(1)    'S' = standard OMOP concept; NULL = non-standard source code
```

### Indexes on `concept`

Two indexes make concept lookups fast:

```sql
-- Primary path: exact vocabulary + code match (covers LOINC, SNOMED, RxNorm lookups)
CREATE INDEX ix_concept_vocab_code ON concept (vocabulary_id, concept_code);

-- Fallback path: name-based search
CREATE INDEX ix_concept_name_trgm ON concept USING gin (concept_name gin_trgm_ops);
```

### Concept search API

The supported API for searching and browsing OMOP concepts is documented in
[API_SURFACE.md](../API_SURFACE.md#vocabulary--concept-lookup-endpoints):

| Endpoint | Use |
|---|---|
| `GET /api/v1/concepts/search/?q=creatinine` | Case-insensitive substring search on `concept_name`, with optional exact filters for `vocabulary_id`, `domain_id`, `concept_class_id`, and `standard_concept` |
| `GET /api/v1/concepts/?domain_id=Measurement&concept_class_id=Lab%20Test` | Filtered concept browsing without a text query |
| `GET /api/v1/concepts/lookup/?lookup=LOINC:2160-0` | Batch translation from `(vocabulary_id, concept_code)` to OMOP `concept_id` |

Search and browse responses are paginated, default to 25 results, cap `page_size` at 100, and
return the same concept fields listed above. The search endpoint requires `q` to be at least
two characters; the browse endpoint requires at least one of `vocabulary_id`, `domain_id`, or
`concept_class_id` so production deployments do not accidentally page across the full Athena
concept table.

---

## Concept graph API

PROMOP exposes the loaded OMOP graph to API consumers:

- `GET /api/v1/concepts/{concept_id}/ancestors/`
- `GET /api/v1/concepts/{concept_id}/descendants/`
- `GET /api/v1/concepts/graph/`

Use these endpoints when a consumer needs runtime traversal instead of relying on PRomop's internal `refresh_patient_record()` expansion.

Traversal rules:

- If `relationship_id` is supplied, PRomop traverses direct `concept_relationship` edges, following stored edge direction: `ancestors` returns in-neighbors (edges pointing *at* the source), `descendants` returns out-neighbors. For OMOP hierarchical relationships authored child → parent (e.g. `Is a`), use closure mode for true ancestor traversal.
- Edges with `invalid_reason` set are excluded from relationship-mode traversal.
- Otherwise PRomop traverses `concept_ancestor` closure rows.
- `max_levels` applies only to `concept_ancestor` traversal.
- `vocabulary_id` and `concept_class_id` filter the returned concepts, not the source concept.
- Results are capped at 1000 nodes per source concept (`truncated` flag in the response); the batch endpoint accepts at most 200 `concept_id` params.

Common HemOnc patterns:

| Use case | Endpoint shape |
|---|---|
| Regimen → component drugs | `GET /api/v1/concepts/{regimen_id}/descendants/?relationship_id=Has targeted therapy` |
| Component drug → class | `GET /api/v1/concepts/{drug_id}/ancestors/?max_levels=1&vocabulary_id=HemOnc` |
| Batch expand multiple trial regimen ids | `GET /api/v1/concepts/graph/?direction=descendants&concept_id=...&relationship_id=...` |

The canonical endpoint contract is documented in [API_SURFACE.md](../API_SURFACE.md#concept-graph-endpoints).

---

## FHIR → OMOP Concept Resolution

### 1. LOINC Observation codes → `measurement_concept_id`

FHIR `Observation` resources carry a LOINC code in `code.coding[system='http://loinc.org']`.
The upload handler (`patient_portal/api/views.py`) resolves these in three tiers:

| Tier | Mechanism | When used |
|---|---|---|
| 1 | `concept_by_loinc(code)` — exact match on `(vocabulary_id='LOINC', concept_code=code)` | LOINC code present and concept loaded |
| 2 | `concept_by_name_ilike(display_name)` — trigram match on `concept_name` | LOINC concept not in local tables; display name present |
| 3 | OMOP `No matching concept` sentinel (concept_id `0`) | No match found |

The resolved concept becomes `Measurement.measurement_concept_id`. The original LOINC code is
also stored as `Measurement.measurement_source_value` so lookups still work if Athena is
later loaded.

**LOINC → PatientRecord field mapping** (50+ codes; see `omop_core/services/mappings.py`):

| LOINC | PatientRecord field |
|---|---|
| `718-7` | `hemoglobin_g_dl` |
| `6690-2` | `wbc_count_thousand_per_ul` |
| `777-3` | `platelet_count_thousand_per_ul` |
| `2160-0` | `serum_creatinine_mg_dl` |
| `17861-6` | `serum_calcium_mg_dl` |
| `1975-2` | `bilirubin_total_mg_dl` |
| `1742-6` | `alt_u_l` |
| `2532-0` | `ldh_u_l` |
| `1952-1` | `beta2_microglobulin` |
| `89247-1` | `ecog_performance_status` |
| `29463-7` | `weight` |

The full mapping covers CBC, comprehensive metabolic panel, liver function, cardiac markers,
coagulation, oncology markers, and vital signs.

### 2. SNOMED Condition codes → `condition_concept_id`

FHIR `Condition` resources optionally carry a SNOMED code in
`code.coding[system='http://snomed.info/sct']`.

The upload handler currently uses name-based lookup
(`concept_name__icontains`) to resolve conditions. If your FHIR Condition resources include
standard SNOMED codes, the lookup follows this chain:

| Tier | Mechanism |
|---|---|
| 1 | `concept_by_vocab('SNOMED', snomed_code)` — exact match when SNOMED vocabulary is loaded |
| 2 | Trigram name match on `concept_name` |
| 3 | Condition dropped (not stored as ConditionOccurrence) |

The disease name string is always stored in `condition_occurrence.condition_source_value`
regardless of whether a standard concept was resolved.

### 3. Drug codes → `drug_concept_id` → HemOnc classification

FHIR `MedicationStatement` resources represent therapy lines. Each MedicationStatement maps to
a `drug_exposure` row and an `episode` row (one episode per line of therapy).

Drug concept resolution uses four tiers:

| Tier | Mechanism | When used |
|---|---|---|
| 1 | HemOnc `concept_id` embedded in `medicationCodeableConcept.coding[system='http://ohdsi.org/omop/HemOnc']` | Bundle generated by PRomop's `generate_fhir_bundle` |
| 2 | `concept_name__icontains(regimen_name)` on `domain='Drug'` | Manual or EHR-sourced bundles |
| 3 | RxNav API call — resolves drug name to RxNorm, then looks up the RxNorm Concept | Tier 2 misses; requires network access |
| 4 | First concept in Drug domain | Last resort; semantic information is lost |

The resolved concept becomes both `drug_exposure.drug_concept_id` and
`episode.episode_object_concept_id`.

---

## HemOnc Classification for Line-of-Therapy Inference

After FHIR ingest, the LOT inference service (`omop_core/services/lot_inference_service.py`)
classifies each drug by its HemOnc drug class using the `concept_relationship` and
`concept_ancestor` tables.

### Classification query chain

```
drug_concept_id
      │
      ▼ ConceptRelationship WHERE relationship_id = 'Maps to'
        AND concept_2.vocabulary_id = 'HemOnc'
      │
      ├── HemOnc concept(s) found
      │         │
      │         ▼ ConceptAncestor — ancestor class names
      │
      │   Match ancestor names against class sets:
      │     HEMONC_MYELOMA_CLASSES  → classify as 'myeloma'
      │     HEMONC_CART_CLASSES     → classify as 'cart'
      │     HEMONC_STEROID_CLASSES  → classify as 'steroid'
      │     (no match)              → classify as 'mixed'
      │
      └── No HemOnc concept found
                │
                ▼ DRUG_SUBTYPE_MAP lookup on drug_source_value string
```

### HemOnc ancestor class sets (examples)

| Class set | Ancestor concept names included |
|---|---|
| Myeloma agents | `Antimyeloma agents`, `Proteasome inhibitors`, `Immunomodulatory agents`, `Monoclonal antibodies` |
| CAR-T | `CAR T-cell therapies`, `Immunotherapy` |
| Steroids | `Corticosteroids`, `Glucocorticoids` |

### Concept tables used

```sql
-- Step 1: Find HemOnc concepts mapped from a drug
SELECT concept_id_2
FROM concept_relationship
WHERE concept_id_1 = :drug_concept_id
  AND relationship_id = 'Maps to'
  AND concept_id_2 IN (
      SELECT concept_id FROM concept WHERE vocabulary_id = 'HemOnc'
  );

-- Step 2: Find ancestor class names
SELECT c.concept_name
FROM concept_ancestor ca
JOIN concept c ON ca.ancestor_concept_id = c.concept_id
WHERE ca.descendant_concept_id = :hemonc_concept_id
  AND ca.min_levels_of_separation > 0;
```

---

## Concept Lookup Cache

All concept lookups are routed through `omop_core/services/concept_cache.py`, a process-level
in-memory cache. This eliminates repeated database round-trips during bulk FHIR imports (a
1,000-patient bundle would otherwise issue thousands of identical Concept queries).

```
concept_cache.py
├── concept_by_id(concept_id)         — PK lookup, cached by int key
├── concept_by_loinc(loinc_code)       — delegates to concept_by_vocab('LOINC', code)
├── concept_by_vocab(vocab_id, code)   — (vocabulary_id, concept_code) tuple key
└── concept_by_name_ilike(name)        — trigram fallback, cached by name string
```

The cache lives for the lifetime of the worker process and is never invalidated — safe because
the Concept table is static (updated only when you load new Athena vocabulary files, which
requires a restart).

---

## PatientRecord Derivation from Measurements

After FHIR ingest stores lab values as `Measurement` rows, `refresh_patient_record()` derives
the PatientRecord lab fields in three lookup tiers:

| Tier | Mechanism | Example |
|---|---|---|
| 1 | `Measurement.measurement_concept.concept_code` in `_LOINC_LAB_FIELDS` | LOINC code on the Concept row |
| 2 | `Measurement.measurement_source_value` in `_LOINC_LAB_FIELDS` | LOINC code stored as source value at ingest time |
| 3 | `Measurement.measurement_source_value` in `_SOURCE_VALUE_LAB_FIELDS` | Display name stored as source value (legacy path) |

The most-recent measurement per LOINC code wins. This means PatientRecord always reflects
current lab values without the caller needing to know which Athena concepts are loaded.

---

## Custom FHIR Extensions

PRomop uses custom FHIR extensions under `https://healthkey.ai/fhir/StructureDefinition/`
to carry data that has no standard FHIR path. These extensions are produced by
`generate_fhir_bundle` and parsed by the `upload_fhir` endpoint.

| Extension URL | Field |
|---|---|
| `.../race` | `Person.race_source_value` |
| `.../ethnicity` | `Person.ethnicity_source_value` |
| `.../bodyWeight` | `PatientRecord.weight` |
| `.../bodyHeight` | `PatientRecord.height` |
| `.../ecog-performance-status` | `PatientRecord.ecog_performance_status` |
| `.../systolic-bp` | `PatientRecord.systolic_blood_pressure` |
| `.../diastolic-bp` | `PatientRecord.diastolic_blood_pressure` |
| `.../heartRate` | `PatientRecord.heartrate` |
| `.../mm-sct-history` | `PatientRecord.stem_cell_transplant_history` |
| `.../mm-sct-date` | `PatientRecord.sct_date` |
| `.../mm-sct-eligibility` | `PatientRecord.sct_eligibility` |
| `.../mm-cytogenetic-markers` | `PatientRecord.cytogenetic_markers` |
| `.../therapy-line` | `Episode.episode_number` (line of therapy number) |
| `.../therapy-outcome` | `Episode` response code |

If you are writing an integration that produces FHIR bundles for PRomop, use these exact URLs.
Standard FHIR paths (LOINC Observations, SNOMED Conditions, RxNorm MedicationStatements) should
use their standard coding systems; these custom extensions are only needed for data that has no
standard FHIR representation.

---

## Loading Athena Vocabulary Tables

PRomop's LOINC and SNOMED lookups require the OHDSI Athena vocabulary tables to be loaded into
your PostgreSQL instance. Without them, the system falls back to generic concept IDs and
name-based matching, which is functional but loses semantic precision.

### Step 1 — Download vocabulary files

1. Go to [athena.ohdsi.org](https://athena.ohdsi.org) and create a free account.
2. Select the vocabularies your deployment needs:
   - **Required for deployed clinical environments**: LOINC, SNOMED CT,
     RxNorm, ICD10CM
   - **Required for PROMOP oncology features**: HemOnc
   - **Included when available**: RxNorm Extension and ATC
3. Download the ZIP — it contains one TSV file per vocabulary table.

### Step 2 — Load into PostgreSQL

```bash
# Unzip to a local directory
unzip athena_download.zip -d /tmp/athena_vocab

# Run the loader (uses PostgreSQL COPY for fast bulk insert)
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_athena_vocabularies \
    --path /tmp/athena_vocab

# Verify
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py shell -c "
from omop_core.models import Concept
print('LOINC:', Concept.objects.filter(vocabulary_id='LOINC').count())
print('SNOMED:', Concept.objects.filter(vocabulary_id='SNOMED').count())
print('RxNorm:', Concept.objects.filter(vocabulary_id='RxNorm').count())
print('ICD10CM:', Concept.objects.filter(vocabulary_id='ICD10CM').count())
"
```

### Step 3 — Deploy note

On Render (or any production platform), vocabulary loading is a one-time operation run after
the first deploy. Upload the Athena TSV files to object storage (GCS or S3) and run:

```bash
python manage.py load_athena_vocabularies --bucket your-bucket-name
```

The command verifies LOINC, RxNorm, SNOMED, and ICD10CM after a non-dry-run
load. `--dry-run` counts records without writing. Avoid `--replace` for a
partial-load repair: it truncates vocabulary tables and cascades to clinical
tables; the normal upsert is safe for an existing clinical environment.

---

## Verification Queries

Use these queries to confirm vocabulary loading and concept resolution are working correctly.

```sql
-- Count loaded concepts per vocabulary
SELECT vocabulary_id, COUNT(*) as concept_count
FROM concept
GROUP BY vocabulary_id
ORDER BY concept_count DESC;

-- Find the LOINC concept for hemoglobin
SELECT concept_id, concept_code, concept_name
FROM concept
WHERE vocabulary_id = 'LOINC' AND concept_code = '718-7';

-- Find HemOnc concepts mapped from a drug concept
SELECT c2.concept_id, c2.concept_code, c2.concept_name
FROM concept_relationship cr
JOIN concept c2 ON cr.concept_id_2 = c2.concept_id
WHERE cr.concept_id_1 = :drug_concept_id
  AND cr.relationship_id = 'Maps to'
  AND c2.vocabulary_id = 'HemOnc';

-- Find ancestor drug classes for a HemOnc concept
SELECT c.concept_name, ca.min_levels_of_separation
FROM concept_ancestor ca
JOIN concept c ON ca.ancestor_concept_id = c.concept_id
WHERE ca.descendant_concept_id = :hemonc_concept_id
ORDER BY ca.min_levels_of_separation;

-- Check what LOINC concepts are present for a patient's measurements
SELECT c.concept_code, c.concept_name, m.value_as_number, m.measurement_date
FROM measurement m
JOIN concept c ON m.measurement_concept_id = c.concept_id
WHERE m.person_id = :person_id
  AND c.vocabulary_id = 'LOINC'
ORDER BY m.measurement_date DESC;
```

---

## Fallback Chains Summary

| Data type | Tier 1 | Tier 2 | Tier 3 | Tier 4 |
|---|---|---|---|---|
| Lab observation | LOINC code → Concept | Display name trigram | concept_id `0` (`No matching concept`) | — |
| Condition | SNOMED code → Concept | Name trigram | Dropped | — |
| Drug/therapy | HemOnc concept_id in FHIR | Drug name trigram | RxNav API lookup | First Drug concept |
| Drug class (LOT) | ConceptRelationship → HemOnc ancestors | Drug source_value string map | `'mixed'` | — |

Labs with no matching LOINC Concept still land in the `measurement` table (with
`measurement_concept_id = 0`) and can be retrieved by
`measurement_source_value`. PatientRecord fields for unmatched labs will be null
until a matching Concept is loaded.
