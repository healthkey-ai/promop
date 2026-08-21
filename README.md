# PRomop

[![CI](https://github.com/healthkey-ai/promop/actions/workflows/ci.yml/badge.svg)](https://github.com/healthkey-ai/promop/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**PRomop** is an open-source longitudinal patient health record built on the [OMOP CDM v5.4](https://ohdsi.github.io/CommonDataModel/) with FHIR R4 ingestion. Its central feature is `PatientRecord` — a wide denormalized projection (300+ columns) derived automatically from OMOP tables that gives analytics, trial matching, and clinical decision support a single shared substrate, eliminating the repeated re-derivation of patient state across applications.

Deployed across approximately 17,500 real oncology patients, with trial matching against 6,000 actively recruiting trials. Benchmarks show a [~37× speedup](https://arxiv.org/abs/2607.13947) for eligibility screening compared to querying raw OMOP tables directly.

See [paper.md](paper.md) for the full research description.

**New here?** → [**Load and query patient data in 10 minutes**](docs/quickstart.md)

Not on a Mac? See the [Linux setup guide](docs/linux-setup.md). Prefer Docker? See [BUILDING_WITH_DOCKER.md](BUILDING_WITH_DOCKER.md).

---

## Key Features

- **FHIR R4 ingestion** — Bundle uploads mapped to OMOP tables (observations → `Measurement`, conditions → `ConditionOccurrence`, medications → `DrugExposure` + `Episode`)
- **PatientRecord projection** — 300+ column decision-ready view, auto-rebuilt via signal chain on every OMOP write
- **Versioned REST API** — `/api/v1/` with [OpenAPI 3.0 schema](API_SURFACE.md) and Swagger UI at `/api/v1/docs/`
- **Multi-tenant access control** — OAuth2 and SMART on FHIR authorization, org-scoped role-based access
- **Synthetic FHIR generator** — reproducible patient bundles for multiple diseases (MM, FL, breast cancer)

---

## API Documentation

- Interactive Swagger UI: `http://localhost:8000/api/v1/docs/`
- OpenAPI schema: `GET /api/v1/schema/`
- Full API surface reference: **[API_SURFACE.md](API_SURFACE.md)**
- LOINC / SNOMED / HemOnc concept mapping: **[docs/concept-mapping.md](docs/concept-mapping.md)**

---

## Local Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL 14+

### 1. Clone and create virtual environment

```bash
git clone https://github.com/healthkey-ai/promop.git
cd promop
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create local databases

```bash
# Start PostgreSQL (Homebrew)
brew services start postgresql@14

# Create role and databases (run once)
PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U $(whoami) -d postgres \
  -c "CREATE ROLE postgres WITH SUPERUSER CREATEDB CREATEROLE LOGIN;"

PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U postgres -d postgres \
  -c "CREATE DATABASE promop_dev OWNER postgres;" \
  -c "CREATE DATABASE promop_test OWNER postgres;"
```

### 3. Apply migrations

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py migrate
```

### 4. Create a superuser

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py setup_admin
```

### 5. Run the backend

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True \
  .venv/bin/python manage.py runserver
```

The API is available at `http://localhost:8000/api/v1/`.

### 6. Run the frontend

```bash
cd frontend
npm ci
npm run dev
```

The UI is available at `http://localhost:5173`.

---

## Docker

See [BUILDING_WITH_DOCKER.md](BUILDING_WITH_DOCKER.md) for the full guide including dev mode,
common tasks, environment variables, and troubleshooting. The short version:

```bash
cp .env.example .env   # set ADMIN_PASSWORD
docker compose up --build
```

---

## Running Tests

There are **three** suites. The backend is split across two runners: Django's runner discovers
only `omop_core.tests` and `patient_portal.tests`, while the `tests/` package is pytest-based and
invisible to it. Run both.

```bash
# Backend — Django runner (omop_core + patient_portal)
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput

# Backend — pytest (the tests/ package)
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" DEBUG=True \
  .venv/bin/python -m pytest -q

# Frontend
cd frontend && npm test -- --run
```

The pytest suite runs with `--no-migrations`, so its database is built by reflecting model state.
That recreates `concept`'s GIN trigram index during `CREATE TABLE`, before any fixture could
enable the extension — so `pg_trgm` must already exist on `template1`, which every new test
database is cloned from. Without this, every pytest test errors with
`operator class "gin_trgm_ops" does not exist`:

```bash
PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH" psql -U postgres -d template1 \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
```

---

## Populating Sample Patient Data

See [docs/sample-patient-data.md](docs/sample-patient-data.md) for instructions on generating and loading synthetic FHIR patient bundles for multiple disease types.

---

## Reproducing Benchmark Results

See **[docs/reproducing-benchmark-results.md](docs/reproducing-benchmark-results.md)** for step-by-step instructions to reproduce the trial-eligibility and full PatientRecord benchmarks from the paper. Two routes: import the published 1,000-patient cohort from Zenodo ([10.5281/zenodo.21430170](https://doi.org/10.5281/zenodo.21430170)), or generate an equivalent one locally with Synthea. Both cohorts are fully synthetic. Includes a complete reference table of every OMOP source (LOINC code, concept name filter, source-value alias) that feeds each PatientRecord column.

---

## Project Structure

| Directory | Purpose |
|---|---|
| `omop_core/` | OMOP CDM models, migrations, PatientRecord projection |
| `omop_oncology/` | Episode, EpisodeEvent, line-of-therapy inference |
| `patient_portal/` | DRF API, FHIR upload, serializers, views |
| `frontend/` | React 18 + TypeScript + Tailwind UI |
| `omop_core/management/commands/` | Management commands (generate, import, backfill) |

---

## Deployment (Render)

`start.sh` runs `migrate` and `setup_admin` on every deploy. Push to `main` to trigger a Render deploy.

- Backend: `https://promop.onrender.com`
- Admin credentials: set via `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars on Render

---

## Contributing and Support

- **Contribute** — see **[CONTRIBUTING.md](CONTRIBUTING.md)** for branch, test, and pull request
  conventions. The `dev` branch is the integration target; `main` is the production branch.
- **Report a bug or request a feature** — open an issue at
  [github.com/healthkey-ai/promop/issues](https://github.com/healthkey-ai/promop/issues).
  Security vulnerabilities go to **support@healthkey.ai** instead of a public issue.
- **Get help** — email **support@healthkey.ai**.

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).

---

## Citation

If you use PRomop in research, please cite it using [CITATION.cff](CITATION.cff) or via GitHub's "Cite this repository" button.
