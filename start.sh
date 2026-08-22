#!/bin/bash
set -e

echo "Running migrations..."
python manage.py migrate --noinput

# Concepts that clinical FKs point at — gender, type concepts, the OMOP "no
# matching concept" sentinel. No migration seeds them, so this path relied on
# someone having run the seeder by hand; a deployment without them writes null
# concepts and derivation silently reads nothing.
#
# Safe under `set -e`: idempotent via get_or_create, and where a concept_id would
# collide with a real Athena row already holding that (vocabulary, code) it skips
# with a warning rather than raising.
echo "Seeding OMOP concepts..."
python manage.py seed_omop_concepts

echo "Creating/resetting admin user..."
python manage.py setup_admin

echo "Starting gunicorn..."
exec gunicorn ctomop.wsgi:application
