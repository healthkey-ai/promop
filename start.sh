#!/bin/bash
set -e

echo "Running migrations..." >&2
python manage.py migrate --noinput >&2

# Concepts that clinical FKs point at — gender, type concepts, the OMOP "no
# matching concept" sentinel. No migration seeds them, so this path relied on
# someone having run the seeder by hand; a deployment without them writes null
# concepts and derivation silently reads nothing.
#
# Safe under `set -e`: idempotent via get_or_create, and where a concept_id would
# collide with a real Athena row already holding that (vocabulary, code) it skips
# with a warning rather than raising.
echo "Seeding OMOP concepts..." >&2
python manage.py seed_omop_concepts >&2

echo "Creating/resetting admin user..." >&2
python manage.py setup_admin >&2

echo "Starting gunicorn..." >&2
exec gunicorn ctomop.wsgi:application \
    --access-logfile /dev/stderr \
    --error-logfile /dev/stderr
