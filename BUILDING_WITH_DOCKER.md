# Running PRomop with Docker

Docker is the fastest way to get PRomop running — no local PostgreSQL, no Python version
management, works the same on Mac, Linux, and Windows.

**Prerequisites:** [Docker Desktop](https://www.docker.com/products/docker-desktop/) (Mac/Windows)
or Docker Engine + Docker Compose v2 (Linux). No other dependencies required.

---

## Quick start (5 minutes)

```bash
# 1. Clone the repo
git clone https://github.com/healthkey-ai/promop.git
cd promop

# 2. Create your environment file
cp .env.example .env
```

Open `.env` and set `ADMIN_PASSWORD` to something you'll remember. Everything else works
as-is for local use. For production, also change `SECRET_KEY` and set `DEBUG=False`.

```bash
# 3. Build and start
docker compose up --build
```

First build takes 3–5 minutes (downloads base images, installs Python and Node dependencies,
builds the React frontend). Subsequent starts take a few seconds.

Once you see:
```
promop_web  | [INFO] Listening at: http://0.0.0.0:8000
```

The app is running:

| URL | What it is |
|---|---|
| `http://localhost:8000/` | React frontend |
| `http://localhost:8000/admin/` | Django admin (use your `.env` credentials) |
| `http://localhost:8000/api/v1/docs/` | Swagger API UI |
| `http://localhost:8000/api/v1/schema/` | OpenAPI 3.0 schema |

---

## Load sample patient data

With the stack running, open a second terminal and generate synthetic patients:

```bash
# Generate 20 multiple myeloma patients
docker exec promop_web python manage.py generate_fhir_bundle \
  --disease mm --count 20 --seed 42 --output /tmp/mm_bundle.json

# Import them into the database
docker exec promop_web python manage.py import_fhir_bundle \
  /tmp/mm_bundle.json --org demo-org --batch-size 5 -v 2
```

Then open `http://localhost:8000/admin/` → **Omop Core → Patient infos** to see the records,
or query the API:

```bash
curl -s -u admin@example.com:changeme \
  http://localhost:8000/api/v1/patient-records/ | python3 -m json.tool | head -40
```

Supported disease types: `mm`, `fl`, `breast-cancer`. See
[SYNTHETIC_PATIENT_GENERATION.md](SYNTHETIC_PATIENT_GENERATION.md) for all options.

---

## Development mode (live reload)

The dev compose file mounts your source tree into the container so code changes are reflected
immediately without rebuilding:

```bash
docker compose -f docker-compose.dev.yml up --build
```

- Django's `runserver` reloads automatically when Python files change
- For frontend changes, run `npm run dev` locally (outside Docker) against the running backend:

```bash
cd frontend
npm ci
npm run dev      # proxies API calls to http://localhost:8000
```

The React dev server is at `http://localhost:5173`.

---

## Common tasks

### Run Django management commands

```bash
# Any manage.py command
docker exec promop_web python manage.py <command>

# Examples
docker exec promop_web python manage.py shell
docker exec promop_web python manage.py migrate --noinput
docker exec promop_web python manage.py generate_fhir_bundle --disease fl --count 50 --output /tmp/fl.json
```

### Run the test suite

```bash
docker exec promop_web python manage.py test omop_core patient_portal --verbosity=2 --noinput
```

### View logs

```bash
# All services
docker compose logs -f

# Backend only
docker compose logs -f web

# Database only
docker compose logs -f db
```

### Open a database shell

```bash
docker exec -it promop_db psql -U promop -d promop
```

### Reset everything (wipe data and start fresh)

```bash
docker compose down -v   # stops containers and deletes the postgres_data volume
docker compose up --build
```

---

## Environment variables

All configuration is read from `.env`. Copy `.env.example` as a starting point.

| Variable | Default | Required | Description |
|---|---|---|---|
| `POSTGRES_USER` | `promop` | yes | PostgreSQL username |
| `POSTGRES_PASSWORD` | `promop` | yes | PostgreSQL password |
| `POSTGRES_DB` | `promop` | yes | Database name |
| `POSTGRES_PORT` | `5432` | no | Host port for PostgreSQL (container always uses 5432 internally) |
| `SECRET_KEY` | — | yes | Django secret key — generate one for production |
| `DEBUG` | `False` | no | Set `True` for development |
| `ADMIN_EMAIL` | `admin@example.com` | yes | Email for the auto-created superuser |
| `ADMIN_PASSWORD` | — | yes | Password for the auto-created superuser |
| `PRODUCTION_URL` | `http://localhost:8000` | no | Canonical URL (used in CORS and CSRF settings) |

### Generating a production SECRET_KEY

```bash
docker run --rm python:3.12-slim python -c \
  "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

---

## Production deployment notes

The production `docker-compose.yml` runs gunicorn with 4 workers. For a production deployment:

1. Set `DEBUG=False`
2. Set a real `SECRET_KEY`
3. Set `PRODUCTION_URL` to your public domain
4. Put a reverse proxy (nginx, Caddy, Render, etc.) in front of port 8000
5. The database volume (`postgres_data`) persists across container restarts — back it up

For Render deployment (the default CI target), see the [README](README.md#deployment-render).

---

## Troubleshooting

**`ADMIN_PASSWORD environment variable is not set`**
→ You forgot to set `ADMIN_PASSWORD` in your `.env` file.

**`port is already allocated`**
→ Something else is using port 8000 or 5432. Change `POSTGRES_PORT` or stop the conflicting
process. To use port 5433 for PostgreSQL, set `POSTGRES_PORT=5433` in `.env`.

**`relation "..." does not exist`**
→ Migrations haven't run yet. Run `docker exec promop_web python manage.py migrate`.

**Frontend shows a blank page or 404**
→ The React build is baked into the image at build time. If you changed frontend code,
rebuild the image: `docker compose up --build`.

**Changes to Python code aren't reflected**
→ In production mode (`docker-compose.yml`) the code is baked into the image — rebuild.
In dev mode (`docker-compose.dev.yml`) the source is volume-mounted, so changes should
reload automatically. If they don't, restart: `docker compose -f docker-compose.dev.yml restart web`.
