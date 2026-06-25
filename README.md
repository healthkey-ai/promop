# promop

A Django + React application for oncology patient data management using the OMOP CDM schema. Accepts FHIR R4 bundle uploads, exposes a DRF REST API, and serves a React TypeScript frontend.

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

Or interactively:

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py createsuperuser
```

### 5. Run the backend

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_dev" \
  DEBUG=True \
  .venv/bin/python manage.py runserver
```

The API is available at `http://localhost:8000/api/`.

### 6. Run the frontend

```bash
cd frontend
npm ci
npm run dev
```

The UI is available at `http://localhost:5173`.

---

## Running Tests

```bash
# Backend
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput

# Frontend
cd frontend && npm test -- --run
```

---

## Project Structure

| Directory | Purpose |
|---|---|
| `omop_core/` | OMOP CDM models, migrations, services |
| `omop_oncology/` | Episode, EpisodeEvent, LOT inference |
| `patient_portal/` | DRF API, FHIR upload, serializers, views |
| `frontend/` | React 18 + TypeScript + Tailwind UI |
| `omop_core/management/commands/` | Management commands (generate, load, backfill) |

---

## Deployment (Render)

`start.sh` runs `migrate` and `setup_admin` on every deploy. Push to `main` to trigger a Render deploy.

- Backend: `https://promop.onrender.com`
- Admin credentials: set via `ADMIN_EMAIL` / `ADMIN_PASSWORD` env vars on Render

---

## Populating Sample Patient Data

See [docs/sample-patient-data.md](docs/sample-patient-data.md) for instructions on generating and loading synthetic FHIR patient bundles.
