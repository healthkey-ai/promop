#!/bin/bash
set -e

# Render's web entrypoint. The preparation sequence lives in
# scripts/prepare-deployment.sh so that Cloud Run's release job can run the same
# steps instead of its own copy — see #1625. Render prepares during web boot;
# Cloud Run prepares in a job gated ahead of the traffic shift.
#
# Render production and staging both use this entrypoint. Staging is the
# promop-staging service on dev; its database comes from Render DATABASE_URL.
# Local staging access uses STAGING_DATABASE_URL in .env, not GCP.
./scripts/prepare-deployment.sh

# No --bind: gunicorn defaults to 0.0.0.0:$PORT when PORT is set, which Render
# sets, and --workers likewise follows WEB_CONCURRENCY. Passing them here would
# override what the platform chose.
echo "Starting gunicorn..."
exec gunicorn promop.wsgi:application
