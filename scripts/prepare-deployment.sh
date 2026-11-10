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
# rather than defaulting. At least one has to be set on *every* target that
# runs this script, which is the trap when pointing a new one at it: the Cloud
# Run job will fail here until its environment carries a variable.
#
# Two sources (#2288): ATHENA_VOCABULARY_LFS_URL is the Git LFS repository that
# publishes the Athena and LOINC ZIPs and is tried first; the Drive folder in
# ATHENA_VOCABULARY_GDRIVE_URL is the fallback when it cannot be reached.
ATHENA_SOURCE_ARGS=()
if [ -n "${ATHENA_VOCABULARY_LFS_URL:-}" ]; then
  ATHENA_SOURCE_ARGS+=(--lfs "$ATHENA_VOCABULARY_LFS_URL")
fi
if [ -n "${ATHENA_VOCABULARY_GDRIVE_URL:-}" ]; then
  ATHENA_SOURCE_ARGS+=(--gdrive "$ATHENA_VOCABULARY_GDRIVE_URL")
fi
if [ "${#ATHENA_SOURCE_ARGS[@]}" -eq 0 ]; then
  echo "ATHENA_VOCABULARY_LFS_URL or ATHENA_VOCABULARY_GDRIVE_URL must name the full Athena vocabulary source before this service can deploy" >&2
  exit 1
fi
echo "Preparing the production database..."
python manage.py prepare_production_database "${ATHENA_SOURCE_ARGS[@]}"

# Record one durable request and publish it to the worker's dedicated Athena
# queue. The worker resolves the Drive identity and performs either the initial
# load or a later insert-only update. No archive download, scan, synonym refresh
# or release hashing is allowed to hold the web deployment open (#1674).
echo "Queuing the Athena vocabulary freshness check..."
python manage.py queue_athena_vocabulary_sync "${ATHENA_SOURCE_ARGS[@]}"

echo "Creating/resetting admin user..."
python manage.py setup_admin

# phr installations' sign-up organizations (family auth, phr#43). A no-op
# unless FAMILY_AUTH_SIGNUP_ORGS names some.
echo "Ensuring the family auth sign-up organizations..."
python manage.py ensure_signup_orgs

# Check the LOINC release version and queue a load when it has moved. The check
# is one small authenticated request; the archive is ~92MB and goes to a Celery
# worker, so this does not hold up a deploy. It never fails the deploy either --
# a stale LOINC table degrades property_for() and unit checks, which is not a
# reason to refuse to serve (#1624).
echo "Checking the LOINC release..."
python manage.py check_loinc_release
