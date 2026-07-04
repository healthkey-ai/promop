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
