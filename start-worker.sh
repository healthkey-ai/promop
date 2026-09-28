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

# Which queues this process drains. Webhook delivery is routed to `webhooks`
# (CELERY_TASK_ROUTES), and the default here consumes both so a single-worker
# deployment keeps working unchanged.
#
# That default does NOT isolate them: one process spends the same slots on
# either queue, and a subscriber that accepts a connection and then stalls
# holds a slot for the request timeout — a slot a clinical task needs. The
# separation is a deployment decision because it costs a process: run a second
# worker with CELERY_WORKER_QUEUES=webhooks and set CELERY_WORKER_QUEUES=celery
# on this one. Do that before enabling webhooks in production; until then,
# delivery and clinical tasks share the pool.
export CELERY_WORKER_QUEUES="${CELERY_WORKER_QUEUES:-celery,webhooks}"

# Beat is a singleton, and it belongs to whichever worker drains the default
# queue. Off unless a deployment asks for it: with the default on, scaling this
# worker to N replicas gives N schedulers, each queueing the recovery sweep
# every minute.
if [[ "${CELERY_EMBEDDED_BEAT:-false}" == "true" ]]; then
    exec celery -A promop worker --loglevel=info --queues="${CELERY_WORKER_QUEUES}" \
        --beat --schedule=/tmp/promop-celerybeat-schedule
fi
exec celery -A promop worker --loglevel=info --queues="${CELERY_WORKER_QUEUES}"
