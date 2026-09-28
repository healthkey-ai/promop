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

echo "Creating/resetting admin user..."
python manage.py setup_admin
