#!/bin/bash
set -e

# Fail the deploy on a misconfigured production environment rather than starting
# with a silent fallback. This is what makes patient_portal.E001/E002/E003 a real
# control: CI runs the same check, but only against CI's own placeholder values,
# which proves nothing about this environment. Runs before migrate so a bad deploy
# stops before touching the database.
#
# Render production and staging both use this entrypoint. Staging is the
# promop-staging service on dev; its database comes from Render DATABASE_URL.
# Local staging access uses STAGING_DATABASE_URL in .env, not GCP.
echo "Running production deploy checks..."
python manage.py check --deploy --fail-level ERROR

: "${ATHENA_VOCABULARY_GDRIVE_URL:?ATHENA_VOCABULARY_GDRIVE_URL must point to the full Athena vocabulary folder before this service can deploy}"
echo "Preparing the production database..."
python manage.py prepare_production_database --gdrive "$ATHENA_VOCABULARY_GDRIVE_URL"

echo "Creating/resetting admin user..."
python manage.py setup_admin

echo "Starting gunicorn..."
exec gunicorn promop.wsgi:application
