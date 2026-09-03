#!/bin/bash
set -e

# Fail the deploy on a misconfigured production environment rather than starting
# with a silent fallback. This is what makes patient_portal.E001/E002/E003 a real
# control: CI runs the same check, but only against CI's own placeholder values,
# which proves nothing about this environment. Runs before migrate so a bad deploy
# stops before touching the database.
#
# Scope, so nobody assumes more coverage than exists: this file is the Render
# service's startCommand (render.yaml, branch main) and is the PRODUCTION path
# only. GCP staging deploys from Dockerfile.gcp, whose CMD is gunicorn directly,
# with migrations in a separate Cloud Run job — start.sh never runs there. So
# staging is NOT gated by this, and production is the first place it can fail a
# deploy. Gating the Cloud Run path needs a change to that job's command.
echo "Running production deploy checks..."
python manage.py check --deploy --fail-level ERROR

# Migration 0201 seeds curated HK-Labs text -> LOINC mappings. Its targets are
# Athena concepts, so it must never run against an empty/partial vocabulary.
# Bring the schema to the migration immediately before it, load the complete
# Athena release, then apply 0201 and every later migration. This deliberately
# replaces the retired seed_omop_concepts command: production must use Athena
# as the single source of truth for standard concepts.
echo "Preparing the schema for the Athena vocabulary load..."
python manage.py migrate omop_core 0200 --noinput

: "${ATHENA_VOCABULARY_GDRIVE_URL:?ATHENA_VOCABULARY_GDRIVE_URL must point to the full Athena vocabulary folder before this service can deploy}"
echo "Loading the full Athena vocabulary before remaining migrations..."
python manage.py load_athena_vocabularies --gdrive "$ATHENA_VOCABULARY_GDRIVE_URL"

echo "Applying migrations that require the Athena vocabulary..."
python manage.py migrate --noinput

echo "Creating/resetting admin user..."
python manage.py setup_admin

echo "Starting gunicorn..."
exec gunicorn ctomop.wsgi:application
