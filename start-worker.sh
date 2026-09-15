#!/bin/bash
set -euo pipefail

# Fail rather than boot a worker that cannot consume the web service's jobs.
: "${CELERY_BROKER_URL:?CELERY_BROKER_URL must be set to the web service broker}"
: "${DATABASE_URL:?DATABASE_URL must match the web service database}"
: "${SECRET_KEY:?SECRET_KEY must match the web service signing key}"

# Each child can load its own embedding model. One child avoids four copies on
# a small Render instance; operators can raise this after measuring memory.
export CELERY_WORKER_CONCURRENCY="${CELERY_WORKER_CONCURRENCY:-1}"
export CELERY_WORKER_PREFETCH_MULTIPLIER="${CELERY_WORKER_PREFETCH_MULTIPLIER:-1}"
# Off unless a deployment asks for it. Beat is a singleton: with the default
# on, scaling this worker to N replicas gives N schedulers, each queueing
# the recovery sweep every minute. The deployment that wants it says so.
if [[ "${CELERY_EMBEDDED_BEAT:-false}" == "true" ]]; then
    exec celery -A promop worker --loglevel=info --beat --schedule=/tmp/promop-celerybeat-schedule
fi
exec celery -A promop worker --loglevel=info
