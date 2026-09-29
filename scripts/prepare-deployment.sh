#!/bin/bash
#
# Everything that must be true before this application serves a request.
#
# Both deployment targets need this sequence, and until now each expressed it
# separately: Render ran it inline in start.sh, while Cloud Run runs a
# <service>-migrate job whose command lives in Terraform in the infra repo — so
# nothing in this repository stated what the two had in common, and nothing
# stopped them drifting apart. This script is that statement. Render invokes it
# from start.sh; the Cloud Run job should invoke it as its command (#1628).
#
# It deliberately does not start a server. Render runs it during web boot and
# execs gunicorn afterwards; Cloud Run runs it as a release-phase job, gated
# before traffic shifts. Keeping the serving out means one script serves both
# shapes, and means running it twice is a no-op rather than two web servers.
#
# Every step here is idempotent, because both targets may run it more than once
# — Render on every instance boot, Cloud Run on every deploy.
set -euo pipefail

# Run from the repository root whatever the caller's working directory is. The
# steps below are bare `python manage.py`, and this script exists to be pointed
# at by another platform's job command -- a Cloud Run job spec of
# command: ["/app/scripts/prepare-deployment.sh"] with no workingDir would
# otherwise fail with "can't open file 'manage.py'", after announcing that it
# was running the deploy checks.
cd "$(dirname "$0")/.."

# Fail the deploy on a misconfigured production environment rather than starting
# with a silent fallback. This is what makes patient_portal.E001/E002/E003 a real
# control: CI runs the same check, but only against CI's own placeholder values,
# which proves nothing about this environment. Runs before migrate so a bad
# deploy stops before touching the database.
echo "Running production deploy checks..."
python manage.py check --deploy --fail-level ERROR

# A deployment with no vocabulary source is a broken deployment, so this aborts
# rather than defaulting. It has to be set on *every* target that runs this
# script, which is the trap when pointing a new one at it: the Cloud Run job
# will fail here until its environment carries the variable.
: "${ATHENA_VOCABULARY_GDRIVE_URL:?ATHENA_VOCABULARY_GDRIVE_URL must point to the full Athena vocabulary folder before this service can deploy}"
echo "Preparing the production database..."
python manage.py prepare_production_database --gdrive "$ATHENA_VOCABULARY_GDRIVE_URL"

# Resolve the selected Drive file without downloading it. A previously applied
# file identity exits here; a changed file is downloaded, fingerprinted, and
# passed through the insert-only Athena loader under a database lock. Existing
# vocabulary and clinical rows are never replaced by this deploy path.
echo "Checking the Athena vocabulary release..."
python manage.py sync_athena_vocabulary --gdrive "$ATHENA_VOCABULARY_GDRIVE_URL" --apply

echo "Creating/resetting admin user..."
python manage.py setup_admin

# Check the LOINC release version and queue a load when it has moved. The check
# is one small authenticated request; the archive is ~92MB and goes to a Celery
# worker, so this does not hold up a deploy. It never fails the deploy either --
# a stale LOINC table degrades property_for() and unit checks, which is not a
# reason to refuse to serve (#1624).
echo "Checking the LOINC release..."
python manage.py check_loinc_release
