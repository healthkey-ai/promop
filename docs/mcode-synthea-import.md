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

Synthea writes individual patient JSON files into `output/fhir/`. PRomop expects a single Bundle, so concatenate them:

```bash
python -c "
import json, glob, sys

entries = []
for path in glob.glob('output/fhir/*.json'):
    bundle = json.load(open(path))
    entries.extend(bundle.get('entry', []))

print(json.dumps({'resourceType': 'Bundle', 'type': 'collection', 'entry': entries}))
" > data/synthea_bc_200.json
```

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
  python manage.py migrate
```

**One step that trips people up:** OMOP CDM is designed to run against the full Athena vocabulary — 7 million+ concept rows that you have to license and download. That's fine for production, but it's a 4 GB download before you can run `manage.py runserver`. PRomop ships a seed command that loads the 53 concepts you actually need to import a breast cancer cohort — gender concepts, the LOINC codes for a standard metabolic panel, SNOMED for primary tumor conditions, receptor status markers, and a handful of CDM metadata concepts:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py seed_omop_concepts
```

This is the minimum viable vocabulary for development and testing. If you later load the full Athena vocabulary, the seed command is safe to re-run — it uses `get_or_create` and won't clobber existing rows.

Create a superuser so you can log into the UI later:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  python manage.py createsuperuser
```

---

## Importing the mCODE bundle

The `import_fhir_bundle` command drives the same upload pipeline as the REST API, but bypasses HTTP and Render's 30-second request timeout. It's the right tool for loading large cohorts from the command line:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
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

After importing 200 patients, fire up the API:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True ALLOWED_HOSTS=localhost CORS_ALLOWED_ORIGINS=http://localhost:3000 \
  python manage.py runserver
```

A few queries worth running to verify the import worked:

```bash
# Patient count and disease distribution
curl -s http://localhost:8000/api/stats/disease/ | python -m json.tool

# Lab fill rates — check that CMP fields are populated
curl -s "http://localhost:8000/api/patients/?disease=breast-cancer&limit=5" \
  | python -m json.tool | grep -E "creatinine|hemoglobin|sodium|potassium"

# Stage distribution
curl -s "http://localhost:8000/api/stats/stage/?disease=breast-cancer" | python -m json.tool
```

With 200 mCODE patients you should see:
- ~65% fill rate on creatinine, BUN, and electrolytes (CMP labs present in most encounters)
- ~95% fill rate on hemoglobin and WBC (CBC is in nearly every Synthea encounter)
- Stage distribution weighted toward stages I–III, as the mCODE module models
- Realistic ER/PR/HER2 receptor status distributions across the cohort

---

## Pointing PRism at the data

PRism is our frontend for navigating individual patient records — tabbed views for labs, therapy lines, biomarkers, and disease history, built for oncology care teams and researchers. PRomop's API *is* PRism's backend — keep the `runserver` process from the previous step running, and start the PRism frontend in a second terminal:

```bash
# Terminal 1 — keep this running
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True ALLOWED_HOSTS=localhost CORS_ALLOWED_ORIGINS=http://localhost:3000 \
  python manage.py runserver

# Terminal 2 — PRism frontend
cd ../prism/frontend
echo "REACT_APP_API_BASE_URL=http://localhost:8000" > .env.local
npm install && npm start
```

Log in with the superuser credentials you created during setup. The patient list populates from the PRomop API; clicking a patient opens the tabbed detail view. The mCODE import populates enough fields — staging, receptor status, therapy lines, CMP labs — to make the views feel live rather than half-empty.

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
