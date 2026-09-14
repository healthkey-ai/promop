# Synthetic Patient Generation

Synthetic patient data is generated as FHIR R4 bundles and loaded via the FHIR import pipeline. This exercises the same ingestion code path used for real data.

---

## Generate a FHIR bundle

The `generate_fhir_bundle` command supports multiple disease types via `--disease`.

```bash
# Multiple myeloma (default count: 200)
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease mm \
    --count 100 \
    --output /tmp/mm_bundle.json

# Follicular lymphoma
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease fl \
    --count 100 \
    --output /tmp/fl_bundle.json

# Breast cancer
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease breast-cancer \
    --count 100 \
    --output /tmp/bc_bundle.json
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--disease` | `breast-cancer` | Disease type: `breast-cancer`, `mm`, `fl` |
| `--count` | `200` | Number of patients to generate |
| `--output` | Per-disease default (see below) | Output file path |
| `--seed` | `42` | Integer seed for reproducibility |
| `--tnbc-ratio` | `0.30` | (breast cancer) Fraction of TNBC patients |
| `--rrmm-ratio` | `0.80` | (mm) Fraction of patients with ≥1 prior therapy line |
| `--watch-wait-ratio` | `0.20` | (fl) Fraction on watch-and-wait at diagnosis |

Default output paths (when `--output` is omitted):

| Disease | Default output |
|---|---|
| `breast-cancer` | `data/synthetic_patients_fhir.json` |
| `mm` | `data/mm_patients_fhir.json` |
| `fl` | `data/fl_patients_fhir.json` |

Pass `--seed` to reproduce the same patient set across runs.

---

## Import a FHIR bundle

The `import_fhir_bundle` command loads a generated bundle into the database, bypassing the HTTP layer and Render's 30-second request timeout.

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py import_fhir_bundle /tmp/mm_bundle.json \
    --org my-org \
    --batch-size 5
```

### Options

| Flag | Default | Description |
|---|---|---|
| `file` | *(required)* | Path to FHIR Bundle JSON file |
| `--org` | — | Org slug to assign all patients to (created if it does not exist) |
| `--batch-size` | `1` | Patients per batch |
| `--start-from` | `0` | Skip first N patients (for resuming after failure) |
| `--email` | First superuser | Admin email to authenticate the import as |

### What the importer does

1. Parses the FHIR Bundle and groups entries by patient
2. Uploads patients in batches via the `upload_fhir` view (same path as real FHIR ingestion)
3. Writes OMOP records: `Person`, `Measurement`, `DrugExposure`, `Episode`, `EpisodeEvent`
4. After each batch completes, runs `refresh_patient_record` and `infer_lot_for_person` to rebuild the `PatientRecord` projection

---

## End-to-end example (local)

```bash
# 1. Generate 50 MM patients
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease mm --count 50 --output /tmp/mm_bundle.json

# 2. Import into local dev DB
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py import_fhir_bundle /tmp/mm_bundle.json \
    --org demo-org --batch-size 10 -v 2
```

### On Render (no virtual environment)

```bash
python manage.py generate_fhir_bundle --disease mm --count 50 --output /tmp/mm_bundle.json
python manage.py import_fhir_bundle /tmp/mm_bundle.json --org demo-org --batch-size 10 -v 2
```

## Backfill patient stage

To backfill sample stages, run this in the **Render `promop-staging` web
service shell** (it uses that service's `DATABASE_URL`):

```bash
python manage.py backfill_sample_patient_stage --confirm
```

Preview with `python manage.py backfill_sample_patient_stage --dry-run`.
The default scope is `synthea-bc`, `synthea-mm`, `synthea-fl`,
`abc-foundation`, and `bbc-foundation` (case insensitive). To select another
sample cohort, pass `--org-slugs my-sample-org`; use `--limit 10` for a small run.
Explicitly selected organizations are treated as synthetic data.

The command uses existing OMOP stages first, then preserves any existing patient
stage in OMOP. When both are missing it supplies a deterministic synthetic stage
appropriate to the sample disease. These fallbacks are **sample values**, not
recovered clinical assessments. They are labeled `sample-patient-stage` with
`synthetic fallback` provenance in the observation qualifier, and use concept 0
(no matching concept) rather than inventing vocabulary entries. Existing stages
are not rerandomized. Real imported stages take precedence over sample fallbacks.
The command reports processed and updated counts and fails if it cannot assign a
stage for an unsupported disease. Each patient commits independently; reruns are safe.

The breast-cancer, MM, and FL enrich commands now ensure durable stages before
refreshing PatientRecord. Their `generate_import_enrich_synthea_*` wrappers
inherit this behavior. FL generation also emits a coded stage observation, and
FHIR imports persist condition-stage assertions for each supported disease so a
later refresh retains them.

The standalone breast-cancer enrichment command now includes `synthea-bc` in
its default organization selection, alongside the older foundation cohorts:

```bash
python manage.py enrich_breast_cancer_omop_data --confirm
```

Organization slugs match case-insensitively. Selection recognizes breast disease
labels (including `Malignant tumor of breast` and `BC`) and non-erroneous OMOP
breast-cancer diagnoses when the patient disease projection is missing or stale.
Explicit `--org-slugs` still limits the cohort; `--person-ids` remains available
for individual patients.

For disease-status assertions, see [Sample patient disease status](docs/sample-patient-disease-status.md).
