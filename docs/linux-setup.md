# Local Setup on Linux

This guide mirrors the Mac setup in the [README](../README.md) but uses `apt` and the
PostgreSQL apt repository instead of Homebrew. Tested on Ubuntu 22.04 / Debian 12.

---

## 1. Install prerequisites

```bash
# Python 3.11+
sudo apt update
sudo apt install -y python3.11 python3.11-venv python3.11-dev

# Node.js 18+ (via NodeSource)
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# PostgreSQL 14
sudo apt install -y postgresql-14 libpq-dev
```

---

## 2. Start PostgreSQL and create databases

```bash
sudo systemctl start postgresql
sudo systemctl enable postgresql   # start on boot

# Create the promop role and databases
sudo -u postgres psql \
  -c "CREATE ROLE postgres WITH SUPERUSER CREATEDB CREATEROLE LOGIN PASSWORD 'postgres';" \
  -c "CREATE DATABASE promop_dev OWNER postgres;" \
  -c "CREATE DATABASE promop_test OWNER postgres;"
```

---

## 3. Clone and install Python dependencies

```bash
git clone https://github.com/healthkey-ai/promop.git
cd promop
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## 4. Apply migrations

```bash
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py migrate
```

---

## 5. Create a superuser

```bash
ADMIN_PASSWORD=yourpassword \
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/promop_dev" \
  .venv/bin/python manage.py setup_admin
```

---

## 6. Run the backend

```bash
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/promop_dev" \
  DEBUG=True \
  .venv/bin/python manage.py runserver
```

The API is available at `http://localhost:8000/api/v1/`.

---

## 7. Run the frontend

```bash
cd frontend
npm ci
npm run dev
```

The UI is available at `http://localhost:5173`.

---

## Running tests

```bash
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput
```

---

## Connecting to PostgreSQL

The `psql` binary on Ubuntu is at the standard path, so no `PATH` prefix is needed:

```bash
psql -U postgres -d promop_dev
```

Everything else — migrations, sample data, the API — works the same as described in the
[README](../README.md) and [quickstart](quickstart.md).
