# Populating Sample Patient Data

Synthetic patient data is generated as FHIR R4 bundles and loaded via the FHIR import pipeline. See [SYNTHETIC_PATIENT_GENERATION.md](../SYNTHETIC_PATIENT_GENERATION.md) for full options and flags.

---

## Quick start

```bash
# Generate 50 multiple myeloma patients
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py generate_fhir_bundle \
    --disease mm --count 50 --output /tmp/mm_bundle.json

# Import into the dev database
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py import_fhir_bundle /tmp/mm_bundle.json \
    --org demo-org --batch-size 10 -v 2
```

Supported disease types: `breast-cancer` (default), `mm`, `fl`.

Pass `--seed <integer>` to `generate_fhir_bundle` to reproduce the same patient set.

## Backfill patient stage

After deploying the update, run this in the **Render `promop-staging` web
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
