# Quickstart: Load and Query Patient Data

This tutorial walks you through generating synthetic FHIR patients, importing them into PRomop,
and querying the resulting `PatientRecord` projections via the API.

**Prerequisites:** local setup complete — database created and the full Athena
vocabulary loaded. The order is required: migrate `omop_core` through `0200`,
load the full Athena release, then apply the remaining migrations. Migration
`0201` seeds HK-Labs-to-LOINC mappings and cannot safely run before its LOINC
concepts exist. See the [README](../README.md) and
[vocabulary guide](vocabularies.md) for the exact commands.

---

## 1. Load the full Athena vocabulary, then finish migrations

For a fresh clinical or production database, use this order. Do not substitute
the retired `seed_omop_concepts` development fixture for a full Athena load.

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py migrate omop_core 0200 --noinput

DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py load_athena_vocabularies --gdrive

DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py migrate --noinput
```

## 2. Start the backend

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  ADMIN_PASSWORD=secret \
  DEBUG=True \
  .venv/bin/python manage.py runserver
```

The API is now available at `http://localhost:8000/api/v1/`.

---

## 3. Generate a synthetic FHIR bundle

PRomop ships a generator that produces realistic FHIR R4 Bundles for multiple disease types.
Start with 10 multiple myeloma patients:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease mm \
    --count 10 \
    --seed 42 \
    --output /tmp/mm_bundle.json
```

The `--seed` flag makes the run reproducible — remove it to get a different set each time.

Each generated patient includes demographics, lab values (CBC, chemistry panel, beta-2
microglobulin, LDH), disease staging (ISS/R-ISS), therapy history, and biomarker results.

Other supported diseases:

```bash
# Follicular lymphoma
.venv/bin/python manage.py generate_fhir_bundle --disease fl   --count 10 --output /tmp/fl_bundle.json

# Breast cancer
.venv/bin/python manage.py generate_fhir_bundle --disease breast-cancer --count 10 --output /tmp/bc_bundle.json
```

---

## 4. Inspect the bundle (optional)

The output is a standard FHIR R4 Bundle. Each patient contributes a `Patient` resource plus
`Observation`, `Condition`, `MedicationStatement`, and `Procedure` resources:

```bash
# Count resource types in the bundle
python3 -c "
import json, collections
b = json.load(open('/tmp/mm_bundle.json'))
counts = collections.Counter(e['resource']['resourceType'] for e in b['entry'])
for rt, n in sorted(counts.items()):
    print(f'  {rt}: {n}')
"
```

Expected output for 10 patients:
```
  Condition: 10
  Encounter: 10
  MedicationStatement: ~20-30
  Observation: ~150-250
  Patient: 10
  Procedure: ~10-20
```

---

## 5. Import the bundle

`import_fhir_bundle` loads the FHIR data directly into OMOP tables, bypassing HTTP timeouts.
Specify an organization slug — it is created automatically if it does not exist:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py import_fhir_bundle /tmp/mm_bundle.json \
    --org demo-org \
    --batch-size 5 \
    -v 2
```

The `-v 2` flag prints a per-patient summary showing how many measurements, drug exposures,
and episodes were written:

```
Patient 1/10 — Smith, Jane
  Measurements: 24   DrugExposures: 3   Episodes: 2
Patient 2/10 — Johnson, Robert
  Measurements: 31   DrugExposures: 4   Episodes: 3
...
Done. Imported 10 patients into org 'demo-org'.
```

After each batch, PRomop automatically:
1. Writes OMOP records: `Person`, `Measurement`, `DrugExposure`, `Episode`, `EpisodeEvent`
2. Runs `refresh_patient_record` to rebuild the `PatientRecord` projection for each patient
3. Runs `infer_lot_for_person` to derive structured therapy lines

---

## 6. Verify in the admin UI

Open `http://localhost:8000/admin/` and log in with your superuser credentials.

Navigate to **Omop Core → Patient records** to see the imported records. Each row is a
`PatientRecord` — click any patient to see the full projection: demographics,
staging, lab values, therapy lines, and biomarkers all in one place.

---

## 7. Query via the API

### Browse interactively

Open `http://localhost:8000/api/v1/docs/` — the Swagger UI lets you explore and call every
endpoint without writing any code.

### Fetch the patient list with curl

```bash
curl -s -u admin@example.com:secret \
  http://localhost:8000/api/v1/patient-records/ | python3 -m json.tool | head -60
```

A single record looks like:

```json
{
  "id": 1,
  "person": 9001,
  "disease": "Multiple Myeloma",
  "stage": "III",
  "patient_age": 67,
  "gender": "M",
  "ecog_performance_status": 1,
  "hemoglobin_level": 9.4,
  "hemoglobin_level_units": "G/DL",
  "platelet_count": 142000,
  "serum_creatinine_level": 1.8,
  "beta2_microglobulin": 4.2,
  "iss_stage": "III",
  "first_line_therapy": "VRd",
  "first_line_start_date": "2023-03-15",
  "first_line_outcome": "PR",
  "stem_cell_transplant_history": ["autologous SCT"],
  "organization": "demo-org",
  ...
}
```

### Filter by disease or org

```bash
# All multiple myeloma patients
curl -s -u admin@example.com:secret \
  "http://localhost:8000/api/v1/patient-records/?disease=Multiple+Myeloma"

# Patients in a specific org
curl -s -u admin@example.com:secret \
  "http://localhost:8000/api/v1/patient-records/?organization__slug=demo-org"
```

### Retrieve a single patient record

```bash
curl -s -u admin@example.com:secret \
  http://localhost:8000/api/v1/patient-records/1/
```

---

## 7. Try another disease

Repeat steps 2–4 with a different disease type and a different org slug to see multiple
cohorts side by side:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease fl --count 10 --seed 42 --output /tmp/fl_bundle.json

DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py import_fhir_bundle /tmp/fl_bundle.json \
    --org demo-org --batch-size 5

# Now query across both disease types
curl -s -u admin@example.com:secret \
  "http://localhost:8000/api/v1/patient-records/" | python3 -c "
import json, sys
patients = json.load(sys.stdin)['results']
for p in patients:
    print(p['disease'], '-', p.get('stage', 'N/A'), '-', p.get('first_line_therapy', 'none'))
"
```

---

## What just happened

The import pipeline mirrors what PRomop does with real EHR data:

```
FHIR Bundle (generated or from an EHR)
        │
        ▼  import_fhir_bundle / upload_fhir endpoint
OMOP tables — Measurement, ConditionOccurrence, DrugExposure, Episode
        │
        ▼  post_save signal chain (automatic)
PatientRecord — 300+ column projection, one row per patient
        │
        ▼
REST API  /api/v1/patient-records/
```

The `PatientRecord` projection is what makes eligibility queries fast: a 20-criterion
search that requires 27–39 joins over raw OMOP runs as a flat predicate over a single table.

---

## Next steps

- **Full API reference:** [API_SURFACE.md](../API_SURFACE.md)
- **LOINC / SNOMED / HemOnc concept mapping:** [docs/concept-mapping.md](concept-mapping.md)
- **Synthetic data options:** [SYNTHETIC_PATIENT_GENERATION.md](../SYNTHETIC_PATIENT_GENERATION.md)
- **Research background:** [paper.md](../paper.md)
- **Deployment to Render:** [README.md § Deployment](../README.md#deployment-render)
