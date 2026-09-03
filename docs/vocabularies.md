# Vocabularies in promop

What each vocabulary is, where it is stored, what its concept codes look like, and what they refer to.

The row counts below are a captured staging snapshot, included to explain the
shape of the loaded vocabulary corpus. They are not a required database name
or deployment hostname; deployments select their PostgreSQL database through
the `DATABASE_URL` environment variable.

Companion documents:
- [concept-mapping.md](concept-mapping.md) — how FHIR codes are resolved to OMOP concepts at ingestion
- [wearable-omop-mapping.md](wearable-omop-mapping.md) — the wearable metric → OMOP mapping specifically

---

## 1. Loading vocabulary data

Loading an Athena vocabulary release is required for every environment that
ingests or serves clinical records. Without it, FHIR imports and clinical
lookups can fail to resolve to OMOP concepts.

The normal load is additive: it inserts missing rows and leaves existing rows
alone. `--replace` first loads the incoming release, then removes only scoped
Athena concepts that are absent from that release. Before a stale concept is
deleted, the loader removes vocabulary graph/support rows that use it and
clears patient-facing references to it (using `NULL`, or concept `0` for a
required OMOP field). It retains patients and clinical events, including their
source values; it never uses `TRUNCATE ... CASCADE`.

### Option A: use the PRomop Google Drive zip

This is the easiest path for local setup and staging repairs. The command
downloads the first zip from the shared folder, extracts it under `/tmp/vocab`,
loads the vocabulary tables, and publishes a `vocabulary_release` manifest.

[Open the supplied vocabulary folder](https://drive.google.com/drive/u/0/folders/1HoRWGepqcH3pMKK03KNb1oWpaVs0Avl7).

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_athena_vocabularies --gdrive
```

To load a specific Google Drive file instead of the first zip in the folder,
pass its Drive URL:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_athena_vocabularies --gdrive "https://drive.google.com/file/d/<file-id>/view"
```

### Option B: download your own OHDSI Athena zip

Use the [OHDSI Athena vocabulary site](https://athena.ohdsi.org/vocabulary/list)
to create a vocabulary download. Select these vocabularies:

| Purpose | Select in Athena |
|---|---|
| Core clinical terminology and mappings | **SNOMED**, **ICD10CM**, **CPT4**, **LOINC**, **RxNorm**, **RxNorm Extension**, **UCUM** |
| Immunizations and visits | **CVX**, **Visit**, **Type Concept** |
| Drug classification and oncology treatment | **ATC**, **HemOnc** |
| Genomics and cancer registry data | **OMOP Genomic**, **ICDO3**, **NCIt**, **Cancer Modifier**, **NAACCR** |
| OMOP metadata and demographics | **Episode**, **CDM**, **Gender**, **Race**, **Ethnicity** |

Athena may include required dependencies in the download; keep them in the zip.
The loader ignores vocabularies outside PROMOP's scope. It also narrows some
selected data: only ATC codes beginning with `L`, selected RxNorm/RxNorm
Extension drug classes, and LOINC concepts in the `Measurement`, `Observation`,
`Meas Value`, `Procedure`, and `Note` domains are loaded.

Load the downloaded zip directly:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_athena_vocabularies \
    --archive ~/Downloads/vocabulary_download_v5.zip
```

If you have already extracted the zip, load the extracted directory instead:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_athena_vocabularies \
    --path ~/Downloads/vocabulary_download_v5
```

### Other loading paths

For Google Cloud Storage, upload the Athena CSV files to a bucket root and load
from the bucket:

```bash
VOCAB_BUCKET=ctomop-staging-vocab \
  python manage.py load_athena_vocabularies --bucket "$VOCAB_BUCKET"
```

For S3, download the zip or extracted directory first, then use `--archive` or
`--path`:

```bash
aws s3 cp s3://your-bucket/vocabulary_download_v5.zip /tmp/vocabulary_download_v5.zip
python manage.py load_athena_vocabularies --archive /tmp/vocabulary_download_v5.zip
```

At minimum, deployed clinical environments must contain **LOINC**, **RxNorm**,
**SNOMED**, and **ICD10CM**. The loader verifies those four after every normal
non-dry-run load.

### UMLS source-release cache (optional)

Athena remains the authoritative UMLS-to-OMOP conversion: raw UMLS RRF files
are never inserted into OMOP tables. To retain source-release provenance beside
each Athena load, configure a UTS API key before running the command:

```bash
UMLS_API_KEY="..." UMLS_CACHE_DIR=/persistent/vocab-cache \
  .venv/bin/python manage.py load_athena_vocabularies --gdrive
```

With `UMLS_API_KEY`, the loader asks the NLM UTS release API for the current
UMLS Full Release, downloads it through the authenticated UTS endpoint, checks
that the zip contains `META/MRCONSO.RRF`, and caches it. Its release URL,
version, size, and SHA-256 are stored in the `VocabularyRelease` manifest. Use
`UMLS_RELEASE_URL` or `--umls-release-url` to pin a specific release (for
example the 2022AA URL); use `--skip-umls-cache` to bypass this optional step.
Choose a persistent, access-controlled `UMLS_CACHE_DIR` in production. API keys
and raw UMLS files must not be committed or served by the application.

## 2. Where vocabulary data lives

Every vocabulary — external or local — lives in the **same set of OMOP tables**. There is no per-vocabulary table; `concept.vocabulary_id` is what separates them.

| Table | Rows | What it holds |
|---|---|---|
| `concept` | 1,979,417 | Every concept from every vocabulary. The central table. |
| `concept_relationship` | 12,861,404 | Pairwise links between concepts (`Maps to`, `Has brand name`, `Targeted therapy of`, …) |
| `concept_synonym` | 2,357,441 | Alternative names for a concept |
| `drug_strength` | 637,112 | Ingredient strengths for drug concepts |
| `concept_ancestor` | 137,547 | Transitive hierarchy, used for roll-up queries ("any PARP inhibitor") |
| `relationship` | 722 | The catalogue of relationship types |
| `concept_class` | 433 | Sub-classification within a vocabulary (`Lab Test`, `Ingredient`, `Regimen`) |
| `domain` | 50 | Which clinical table a concept belongs in (`Condition`, `Drug`, `Measurement`, …) |
| `vocabulary` | 27 | The catalogue of vocabularies themselves |
| `source_to_concept_map` | 0 | Legacy OMOP mapping table; unused here |

### The four columns that matter on `concept`

| Column | Meaning |
|---|---|
| `concept_id` | Surrogate primary key. **Assigned by OHDSI**, not derived from the code. |
| `vocabulary_id` + `concept_code` | The **natural key** — the real identity of a concept. Resolution should always use both; a bare `concept_code` is ambiguous because 852 codes are reused across vocabularies. |
| `domain_id` | Which clinical table the concept belongs in |
| `standard_concept` | `'S'` = Standard (use in `*_concept_id`), `'C'` = Classification (hierarchy only), `NULL` = non-standard source concept (use in `*_source_concept_id`, map onward via `Maps to`) |
| `source` | Provenance: `NULL` = loaded from an external vocabulary release, `'HealthKey'` = authored locally |

---

## 3. External vocabularies (loaded from OHDSI Athena)

Loaded by `load_athena_vocabularies`. `source` is `NULL` for all of these.

| Vocabulary | Concepts | Domains | Code format | What the codes refer to |
|---|---|---|---|---|
| **SNOMED** | 1,092,167 | Condition, Observation, Procedure, Device, Drug, Spec Anatomic Site | numeric, e.g. `32485007` | Comprehensive clinical terminology. **Standard for the Condition domain.** Not just diagnoses — it also covers procedures, findings, body structures, organisms and substances. Names carry a semantic tag: `(disorder)`, `(procedure)`, `(finding)`. |
| **RxNorm Extension** | 371,311 | Drug | `OMOP…`, e.g. `OMOP4873974` | OHDSI-authored drug concepts for products that RxNorm (a US vocabulary) does not cover. Standard for Drug alongside RxNorm. |
| **LOINC** | 277,784 | Measurement, Meas Value, Observation, Note | `nnnnn-n`, e.g. `94762-2`; answers are `LA…`, e.g. `LA28366-5` | Laboratory tests **and** clinical observations, survey instruments and document types. **Standard for the Measurement domain.** Not only labs — the wearable metrics use LOINC codes, and `Meas Value` entries are answer-list values. |
| **RxNorm** | 148,875 | Drug | numeric, e.g. `26744` | US drug terminology — ingredients, clinical drugs, brand names. **Standard for the Drug domain.** |
| **ICD10CM** | 73,484 | Condition, Observation, Measurement | `J11.1` | US clinical modification of ICD-10. **Non-standard** — billing codes, mapped onward to SNOMED. |
| **HemOnc** | 13,386 | Drug, Condition, Procedure, Episode | numeric, e.g. `1091` | Haematology/oncology terminology: antineoplastic agents and, importantly, **treatment regimens**. Regimen concepts are Standard for the Episode domain; the drug-level concepts are mostly non-standard and map to RxNorm. |
| **UCUM** | 1,128 | Unit | e.g. `{ai}`, `mg/dL` | Units of measure, used in `unit_concept_id`. |
| **ATC** | 482 | Drug | e.g. `L01XE52` | WHO Anatomical Therapeutic Chemical classification. Classification concepts (`'C'`), used for drug-class roll-ups rather than stored directly. |
| **CVX** | 297 | Drug | numeric, e.g. `03`, `133` | CDC "Vaccine Administered" code set — the US standard for identifying vaccines, carried by FHIR `Immunization` resources. `03` = MMR, `133` = pneumococcal conjugate PCV 13. |

### OMOP type-concept vocabularies

These describe *how a record was captured*, not what it means clinically. Codes are OMOP-generated (`OMOP4822158`).

| Vocabulary | Concepts | Used in |
|---|---|---|
| `Type Concept` | 81 | the generic `*_type_concept_id` set (`EHR` 32817, `Lab` 32856, `Survey` 32883) |
| `Condition Type` | 113 | `condition_occurrence.condition_type_concept_id` |
| `Procedure Type` | 97 | `procedure_occurrence.procedure_type_concept_id` |
| `Observation Type` | 29 | `observation.observation_type_concept_id` |
| `Drug Type` | 16 | `drug_exposure.drug_type_concept_id` |
| `Meas Type` | 12 | `measurement.measurement_type_concept_id` |
| `Episode`, `Visit` | 20 + | `episode.episode_concept_id`, `visit_occurrence.visit_concept_id` |

### The `None` vocabulary

Holds OMOP's universal sentinel, **`concept_id = 0` "No matching concept"**, written to any `*_concept_id` when source data cannot be mapped. Domain-agnostic by design. On staging it is referenced by 12,352 observations, 8,131 drug exposures, 290 conditions and 14 measurements.

---

## 4. Local vocabularies (authored here)

These are **not** from Athena. Rows should carry `source='HealthKey'`, live in an `HK-*` vocabulary, and use a `concept_id >= 2,000,000,000` — the range OHDSI reserves for custom concepts. `seed_omop_concepts._assert_local_mint_convention` enforces this for seeded rows.

| Vocabulary | Concepts | Code format | What it holds |
|---|---|---|---|
| **HK-Labs** | 71 | `hkl:<slug>`, e.g. `hkl:sars-cov-2-naa` | Lab test names extracted from source data with no LOINC match. Quarantined so they do not pollute LOINC. |
| **HK-Regimen** | 7 | `hkr:<slug>`, e.g. `hkr:t-dm1` | Treatment regimens with no HemOnc match. |
| **HK-Wearable** | 5 | `HK-WEAR-<METRIC>`, e.g. `HK-WEAR-STEP-LENGTH` | Wearable metrics with no LOINC equivalent — walking step length, double-support percentage, walking heart rate, basal energy expenditure. |

### Non-conforming local vocabularies

These predate the convention and do **not** follow it. They are documented here because they appear in real queries.

| Vocabulary | Concepts | Code format | Origin |
|---|---|---|---|
| `LOCAL` | 28 | `SYNTH-MM-BMBX`, `FHIR-NOTE-TYPE` | Synthetic-enrichment scaffolding and FHIR import placeholders |
| `FHIR` | 7 | `FHIR-VISIT-AMB` | Visit-type concepts minted during FHIR import (`vocabulary_version = 'local'`) |
| `sct` | 11 | numeric SNOMED codes, e.g. `254837009` | **Not a vocabulary.** `sct` is the short form of the FHIR system URI `http://snomed.info/sct`. These are SNOMED concepts minted under the URI fragment instead of being resolved against `SNOMED`. |

---

## 5. Which vocabulary each clinical table actually uses

Observed on staging — the top three per table.

| Table | `*_concept_id` populated from |
|---|---|
| `condition_occurrence` | SNOMED (2,005), HemOnc (1,000), `None`/unmapped (290) |
| `drug_exposure` | `None`/unmapped (8,131), HemOnc (4,497), RxNorm (3,405) |
| `measurement` | LOINC (194,413), HK-Wearable (26), `None`/unmapped (14) |
| `observation` | LOINC (33,170), `None`/unmapped (12,352), SNOMED (4,953) |
| `procedure_occurrence` | SNOMED (1,762), `None`/unmapped (406), `sct` (66) |
| `episode` | Episode (5,297) |

The high unmapped counts in `drug_exposure` and `observation` are source data that ingestion could not resolve, not corruption.

---

## 6. Rules worth knowing

**Resolve by `(vocabulary_id, concept_code)`, never by `concept_code` alone.** 852 codes appear in more than one vocabulary — `1` exists in both HemOnc and UCUM, `1001` in both HemOnc and RxNorm.

**Never resolve by `concept_name`.** Name matching is how `seed_omop_concepts` came to map LOINC `10839-9` (Troponin I) to a concept named Troponin T.

**`concept_id` is never derived from `concept_code`.** Minting a row at `concept_id = int(concept_code)` creates a duplicate that shadows the genuine concept and poisons `MAX(concept_id)`, which the PK sequence then adopts. See issue #452.

**Vocabulary and domain are orthogonal.** A LOINC code can be `Measurement` domain or `Observation` domain; the vocabulary says which code system defines the term, the domain says which table the data belongs in.

**`standard_concept` is a curation decision, not a quality flag.** Only OHDSI assigns `'S'`. A locally-minted concept claiming it is invisible to `concept_ancestor` roll-ups while appearing standard to tooling. See issue #453.

---

## 7. Known defects

| Issue | Summary |
|---|---|
| [#415](https://github.com/healthkey-ai/promop/issues/415) | Duplicate `(vocabulary_id, concept_code)` pairs; the `UNIQUE` constraint that would prevent them |
| [#450](https://github.com/healthkey-ai/promop/issues/450) | Regimen concepts minted with a fabricated `concept_code` |
| [#451](https://github.com/healthkey-ai/promop/issues/451) | Smoking status stored as a non-standard concept rather than a question/answer pair |
| [#452](https://github.com/healthkey-ai/promop/issues/452) | 25 concepts using their own code as their `concept_id` |
| [#453](https://github.com/healthkey-ai/promop/issues/453) | Six code paths minting concepts that claim `standard_concept='S'` without `source='HealthKey'` |

---

## 8. Loading and seeding

See [Loading vocabulary data](#1-loading-vocabulary-data) for the Google Drive,
Athena zip, GCS, and S3 loading paths. These are the supporting commands used
around the vocabulary load:

| Command | What it does |
|---|---|
| `load_athena_vocabularies` | Loads the full Athena release into `concept` and its support tables, then runs `load_mappings` |
| `load_mappings` | Loads approved code-to-concept mappings from `omop_core/data/code_concept_mappings.json` into SCCM |
| `build_crossmap_artifact` | Rebuilds the mapping artifact from upstream repos (HT-One, HT-Next, hk-labs) |
| `seed_omop_concepts` | Seeds the minimal concept set for dev/test, using **genuine Athena `concept_id`s** so it cannot manufacture duplicates on a database that already has the vocabulary |
| `backfill_concept_source` | Fills `source='HealthKey'` on locally-minted rows, dry-run by default |

### Required clinical vocabulary verification

PROMOP requires **LOINC**, **RxNorm**, **SNOMED**, and **ICD10CM** in every
environment that ingests or serves clinical records. LOINC and RxNorm alone
are insufficient: SNOMED backs standard condition/procedure concepts and
ICD10CM backs EHR diagnosis codes before they map to SNOMED.

`load_athena_vocabularies` now verifies those four vocabularies after every
non-dry-run load and fails before publishing a vocabulary release if one is
missing. This makes a partial Athena bundle or an incomplete load visible
immediately rather than allowing clinical lookups to silently return `null`.

For partial vocabulary repairs, rerun the normal additive load from
[Loading vocabulary data](#1-loading-vocabulary-data). `--skip-clinical-vocabulary-verification` is reserved for
intentionally minimal local/test bundles and must not be used for deployed
clinical environments.

### Code-to-concept mappings

After loading the Athena vocabulary release, `load_athena_vocabularies`
automatically runs `load_mappings` to populate `SourceCodeConceptMapping`
with approved cross-vocabulary mappings from the bundled artifact
(`omop_core/data/code_concept_mappings.json`).

The artifact consolidates mappings from all provenance sources:

| Origin | Mapping type | Approx. count |
|--------|-------------|---------------|
| HT-One | CPT4→SNOMED, ICD10→SNOMED, SNOMED→RxNorm, MedDRA→SNOMED | ~83,914 |
| HK-Labs | Uncoded lab text→LOINC | ~147 |
| HK-Wearable | Apple/Garmin device codes→LOINC/HK-Wearable | ~28 |

Only approved (unambiguous) mappings are loaded by default. To also load
proposed mappings, run `load_mappings --include-proposed` manually.

To rebuild the artifact from upstream sources (requires access to
HT-One, HT-Next, hk-labs repos):

```bash
.venv/bin/python manage.py build_crossmap_artifact
```

To load mappings independently of a vocabulary load:

```bash
.venv/bin/python manage.py load_mappings
```

To check what a code means:

```sql
SELECT concept_id, vocabulary_id, concept_code, domain_id, standard_concept, concept_name
FROM concept
WHERE vocabulary_id = 'SNOMED' AND concept_code = '254837009';
```

To find a Standard concept for a non-standard one:

```sql
SELECT t.concept_id, t.vocabulary_id, t.concept_code, t.concept_name
FROM concept_relationship cr
JOIN concept t ON t.concept_id = cr.concept_id_2
WHERE cr.concept_id_1 = <non-standard concept_id>
  AND cr.relationship_id = 'Maps to'
  AND t.standard_concept = 'S'
  AND cr.invalid_reason IS NULL AND t.invalid_reason IS NULL;
```
