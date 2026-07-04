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
4. After each batch completes, runs `refresh_patient_info` and `infer_lot_for_person` to rebuild the `PatientRecord` projection

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
