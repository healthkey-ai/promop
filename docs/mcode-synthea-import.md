# Loading Synthetic mCODE Breast Cancer Patients into PRomop

*How we went from a pile of real-world oncology FHIR data nobody could share to a reproducible open dataset anyone can run locally — and what it unlocks for cancer informatics.*

---

## The problem with cancer data

If you've tried to build anything serious on top of oncology clinical data, you've run into the same wall. The data exists — hospitals are drowning in it — but it's locked behind IRBs, BAAs, de-identification pipelines, and data use agreements that can take months to negotiate. By the time you have data you can actually load into your development environment, the prototype you wanted to validate is long stale.

This creates a perverse situation: the people most motivated to build tools that help cancer patients are exactly the people who can't get access to cancer data. So they build on synthetic data that doesn't look like real oncology data, or they build on OMOP data that's been scrubbed of the disease-specific richness that makes oncology different from general medicine.

mCODE changes this. And Synthea makes it reproducible. And PRomop gives you somewhere to put it.

---

## What mCODE actually is (and isn't)

mCODE — *minimal Common Oncology Data Elements* — is a FHIR Implementation Guide maintained by MITRE and HL7. It defines what a cancer patient record *should* look like in FHIR R4: which SNOMED codes represent primary tumor conditions, how TNM staging is expressed as a LOINC observation, how treatment lines are structured, what receptor status looks like for breast cancer.

What mCODE is *not* is a data standard invented by a committee that nobody uses. It's been adopted by the Integrated Canopy platform, included in Da Vinci use cases, and is the basis for several real-world oncology data exchange programs. When you build against mCODE, you're building against something that looks like production oncology data — not a toy schema.

The practical implication: Synthea, the open-source synthetic patient generator from MITRE, can produce mCODE-conformant FHIR bundles. Hundreds of breast cancer patients, complete with labs, medication histories, TNM stage, receptor status, and realistic clinical timelines — generated in seconds, freely shareable, zero PHI.

---

## Where OMOP fits in

FHIR and OMOP solve different problems. FHIR is a data *exchange* format — it's how a hospital sends you a patient record. OMOP CDM is a *research* format — it's the schema you use once you have the data, to run cohort queries, survival analyses, and population-level statistics across multiple institutions.

The missing piece is the translation layer. PRomop is that layer for oncology. It's an open-source Django application that accepts FHIR R4 bundles and writes the clinical data into an OMOP CDM v5.4 PostgreSQL schema, then materializes a `PatientInfo` read model that surfaces the oncology-specific fields — disease stage, receptor status, line-of-therapy summaries, lab trends — that general-purpose OMOP tools tend to bury or miss entirely.

The result is something you can actually query: "Give me all stage III breast cancer patients with ER+ status who received a platinum-based regimen in the first line and had a creatinine above 1.2 before starting treatment." That query runs in milliseconds on a local PostgreSQL instance loaded from synthetic Synthea data.

---

## Generating the synthetic cohort

Synthea ships with an mCODE breast cancer module. You'll need Java 11+ and the Synthea jar:

```bash
# Download Synthea (check https://github.com/synthetichealth/synthea for latest)
wget https://github.com/synthetichealth/synthea/releases/latest/download/synthea-with-dependencies.jar
```

Generate 200 breast cancer patients. The `--exporter.fhir.export true` flag produces R4 bundles; `BreastCancer` loads the mCODE-aligned disease module:

```bash
java -jar synthea-with-dependencies.jar \
  -p 200 \
  -m breast_cancer \
  --exporter.fhir.export true \
  --exporter.fhir.use_us_core_ig true \
  Massachusetts
```

Synthea writes individual patient JSON files into `output/fhir/`. PRomop expects a single Bundle, so concatenate them. Run this from the Synthea directory, pointing the output at your PRomop `data/` folder:

```bash
# Run from the Synthea directory
python -c "
import json, glob

entries = []
for path in glob.glob('output/fhir/*.json'):
    bundle = json.load(open(path))
    entries.extend(bundle.get('entry', []))

print(json.dumps({'resourceType': 'Bundle', 'type': 'collection', 'entry': entries}))
" > /path/to/promop/data/synthea_bc_200.json
```

Replace `/path/to/promop` with the absolute path to your PRomop clone.

---

## Setting up PRomop locally

Clone the repo and set up the environment:

```bash
git clone https://github.com/healthkey-ai/promop.git && cd promop
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create a dev database and apply migrations:

```bash
createdb -U postgres promop_dev
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True SECRET_KEY=dev-only-secret \
  python manage.py migrate
```

**One step that trips people up:** OMOP CDM is designed to run against the full Athena vocabulary — 7 million+ concept rows that you have to license and download. That's fine for production, but it's a 4 GB download before you can run `manage.py runserver`. PRomop ships a seed command that loads the 53 concepts you actually need to import a breast cancer cohort — gender concepts, the LOINC codes for a standard metabolic panel, SNOMED for primary tumor conditions, receptor status markers, and a handful of CDM metadata concepts:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True SECRET_KEY=dev-only-secret \
  python manage.py seed_omop_concepts
```

This is the minimum viable vocabulary for development and testing. If you later load the full Athena vocabulary, the seed command is safe to re-run — it uses `get_or_create` and won't clobber existing rows.

Create a superuser so you can log into the UI later:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True SECRET_KEY=dev-only-secret \
  python manage.py createsuperuser
```

---

## Importing the mCODE bundle

The `import_fhir_bundle` command drives the same upload pipeline as the REST API, but bypasses HTTP and Render's 30-second request timeout. It's the right tool for loading large cohorts from the command line:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True SECRET_KEY=dev-only-secret \
  python manage.py import_fhir_bundle data/synthea_bc_200.json --batch-size 20
```

You'll see batched output as patients are processed. The importer:

1. Parses Patient, Condition, Observation, MedicationStatement, and MedicationRequest resources from the bundle
2. Writes each to the appropriate OMOP CDM table (Person, ConditionOccurrence, Measurement, DrugExposure, Episode)
3. Expands BP panel observations (LOINC 85354-9) into individual systolic/diastolic Measurement rows
4. Parses US Core race/ethnicity nested extensions
5. Calls `refresh_patient_info` once per patient to rebuild the `PatientInfo` read model from the OMOP tables

The mCODE FHIR bundle uses a handful of LOINC codes that differ from standard (e.g., `38483-4` for creatinine in blood vs. `2160-0` for creatinine in serum/plasma). PRomop maps both, so your CMP labs fill at the same rates you'd expect from a non-mCODE bundle.

---

## What you get

After importing 200 patients, verify the import worked:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True SECRET_KEY=dev-only-secret \
  python manage.py shell -c "
from omop_core.models import Person, PatientInfo
print('Persons imported:', Person.objects.count())
print('PatientInfo records:', PatientInfo.objects.count())

from django.db.models import Count
stages = PatientInfo.objects.exclude(disease_stage='').exclude(disease_stage=None) \
    .values('disease_stage').annotate(n=Count('id')).order_by('-n')
print('Stage distribution:')
for s in stages:
    print(f'  {s[\"disease_stage\"]}: {s[\"n\"]}')

filled = PatientInfo.objects.exclude(creatinine=None).count()
total = PatientInfo.objects.count()
print(f'Creatinine fill rate: {filled}/{total} ({100*filled//total if total else 0}%)')
"
```

With 200 mCODE patients you should see:
- ~65% fill rate on creatinine, BUN, and electrolytes (CMP labs present in most encounters)
- ~95% fill rate on hemoglobin and WBC (CBC is in nearly every Synthea encounter)
- Stage distribution weighted toward stages I–III, as the mCODE module models
- Realistic ER/PR/HER2 receptor status distributions across the cohort

---

## Pointing PRism at the data

PRism is a read-only oncology analytics platform — a cohort builder and dashboard suite that sits on top of the OMOP CDM data that PRomop manages. Where PRomop handles ingestion and exposes a per-patient REST API, PRism is for population-level analysis: filtering a cohort by 20+ clinical criteria, then visualizing treatment patterns, response rates, survival curves, staging distributions, lab value trends, and therapy sequences across the cohort.

```
Synthea FHIR bundles
        │
        ▼
  PRomop (Django + PostgreSQL)
  ├── OMOP CDM tables (Person, Measurement, ConditionOccurrence, DrugExposure …)
  └── PatientInfo (286-column denormalized projection)
        │  read-only, managed=False
        ▼
  PRism backend (Django, port 8000)
        │
        ▼
  PRism frontend (React/Vite, port 5173)
  ├── Cohort builder (clinical criteria filters)
  └── Dashboard panels (response rates, treatment patterns, survival, staging …)
```

PRism reads `PatientInfo` directly from PRomop's PostgreSQL database using `managed=False` Django models — no REST calls between the two backends. The PRomop REST API remains the integration point for external tools; PRism bypasses it for query performance.

To run the full stack, you need three terminals:

```bash
# Terminal 1 — PRomop REST API (port 8001; PRism backend claims 8000)
cd /path/to/promop
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True ALLOWED_HOSTS=localhost \
  CORS_ALLOWED_ORIGINS="http://localhost:5173" \
  SECRET_KEY=dev-only-secret \
  python manage.py runserver 8001

# Terminal 2 — PRism backend (port 8000; Vite proxy forwards /api/* here)
cd ~/prism/backend
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True SECRET_KEY=dev-only-secret \
  python manage.py runserver 8000

# Terminal 3 — PRism frontend
cd ~/prism/frontend
npm install && npm run dev
```

Open `http://localhost:5173/` in your browser. Log in with the superuser credentials you created during PRomop setup. The cohort builder loads all patients from the shared `PatientInfo` table; applying filters narrows the cohort and updates the dashboard panels in real time. With 200 mCODE breast cancer patients you have enough density to see meaningful distributions — staging breakdowns, receptor status frequencies, treatment pattern counts, and lab value ranges that reflect the mCODE module's realistic clinical modeling.

---

## Why this matters more than it looks

Building a reproducible synthetic oncology dataset sounds like a developer convenience. It's actually more than that.

Historically, one of the main barriers to open-source oncology tooling has been that you can't ship a tool alongside realistic test data. You can ship unit tests with mocked data, but you can't say "clone this repo, run these two commands, and you have 200 breast cancer patients with realistic lab trends and treatment histories to explore." That gap has meant that oncology informatics tools have mostly been built inside institutions, on institutional data, by people who happen to have access — which skews both who builds them and what they prioritize.

A seeded PRomop instance loaded from Synthea mCODE data changes that calculus. Anyone can run it. Anyone can contribute a feature and verify it against a realistic cohort. Anyone can reproduce a bug report. The data is the same everywhere.

There's more work to do — mCODE FL and MM profiles, fuller Synthea coverage of treatment response, better mapping of TNM sub-staging — but the foundation is there.

---

## Get involved

PRomop is open source under the Apache 2.0 license. The mCODE import pipeline lives in the `omop_core` app; the `PatientInfo` service and FHIR upload handler are the two most active areas.

- **GitHub:** [healthkey-ai/promop](https://github.com/healthkey-ai/promop)
- **Issues:** bug reports, feature requests, and LOINC mapping gaps all welcome
- **CLAUDE.md:** the repo's contribution guide is unusually detailed — read it before opening a PR and you'll save yourself a round-trip

If you work in cancer informatics and have opinions about what belongs in a minimal oncology OMOP schema, we especially want to hear from you. The hardest problems in this space aren't technical — they're about which fields matter enough to standardize.
