# Populating Sample Patient Data

Synthetic patient data is generated as FHIR R4 bundles and loaded via the FHIR upload pipeline. This ensures the same code path used for real data ingestion is exercised with test data.

---

## Generate a FHIR bundle

### Multiple myeloma patients

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_mm_fhir_bundle \
    --count 50 \
    --output /tmp/mm_bundle.json
```

Each run produces a unique set of patients (random seed by default). To reproduce the same set, pass `--seed <integer>`.

Options:

| Flag | Default | Description |
|---|---|---|
| `--count` | 100 | Number of patients to generate |
| `--output` | `data/mm_patients_fhir.json` | Output file path |
| `--seed` | random | Integer seed for reproducibility |
| `--rrmm-ratio` | 0.80 | Fraction of patients with ≥1 prior line of therapy |

### Breast cancer patients

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --count 50 \
    --output /tmp/bc_bundle.json
```

---

## Load a FHIR bundle

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_fhir_bundle /tmp/mm_bundle.json \
    --org abc-foundation \
    --batch-size 10
```

The `--org` slug is created automatically if it does not exist.

Options:

| Flag | Default | Description |
|---|---|---|
| `--org` | *(required)* | Organization slug to assign patients to |
| `--batch-size` | 10 | Patients per upload batch |
| `-v 2` | — | Verbose: show per-patient measurement/drug/episode counts |

### On Render (no virtual environment)

```bash
python manage.py generate_mm_fhir_bundle --count 50 --output /tmp/mm_bundle.json

python manage.py load_fhir_bundle /tmp/mm_bundle.json --org abc-foundation --batch-size 10 -v 2
```

---

## One-liners (local)

```bash
# Generate
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" .venv/bin/python manage.py generate_mm_fhir_bundle --count 50 --output /tmp/mm_bundle.json

# Load
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" .venv/bin/python manage.py load_fhir_bundle /tmp/mm_bundle.json --org abc-foundation --batch-size 10 -v 2
```

---

## What the loader does

1. Parses the FHIR Bundle and groups entries by patient ID
2. Uploads patients in batches via the `upload_fhir` API endpoint
3. Writes OMOP records: `Person`, `Measurement`, `DrugExposure`, `Episode`, `EpisodeEvent`
4. After all batches complete, runs `refresh_patient_info` and `infer_lot_for_person` for every loaded patient to rebuild the `PatientInfo` read model
