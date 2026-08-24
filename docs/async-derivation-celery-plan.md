# Async PatientRecord Derivation with Celery

## Problem

`POST /api/v1/patient-records/{person_id}/refresh/` derives inline. Cost is
O(rows the person holds) — 15-25 s on a bulk-loaded patient, and the endpoint's
25 s `statement_timeout` turns the slowest ones into a 500. A gunicorn worker is
blocked throughout.

## Design

`refresh/` queues a Celery task and returns `202 {task_id}`. The caller polls
`GET /api/v1/derivation-status/{task_id}/`, which reports Celery's own task
state (`PENDING`/`STARTED`/`SUCCESS`/`FAILURE`) and, on failure, the error.

Redis is both broker and result backend. No job table: the task is idempotent —
derivation clears and rebuilds every field — so a duplicate call is extra load,
not a correctness problem, and nothing needs deduplicating.

Only `refresh/` changes. The signal path and `_bulk_write` keep deriving inline.

## DI seam

`.delay()` appears only in `omop_core/tasks.py`. The view depends on:

```python
class DerivationDispatcher(Protocol):
    def dispatch(self, person: Person) -> str:
        """Arrange for the derivation to run, return the task id."""

    def status(self, task_id: str) -> DerivationStatus:
        """Where that derivation got to."""
```

`status` is on the seam too, otherwise the status view reaches into Celery
directly and its tests are back to patching Celery internals.

- `CeleryDispatcher` — `.delay()` on commit. Enqueueing inside the transaction
  lets the worker read uncommitted data.
- `InlineDispatcher` — derives synchronously. Local dev and CI, where there is
  no broker.
- `FakeDispatcher` — records calls, derives nothing. For tests.

`get_dispatcher()` returns Celery when `CELERY_BROKER_URL` is set, Inline
otherwise. Tests swap it with the `use_dispatcher(fake)` context manager from
the same module.

## Code changes

| File | Change |
|---|---|
| `ctomop/celery.py`, `ctomop/__init__.py` | Celery app + export |
| `omop_core/tasks.py` | `refresh_patient_record_task(person_id)` |
| `omop_core/services/derivation_jobs.py` | Protocol, three dispatchers, `get_dispatcher()` |
| `patient_portal/api/views.py` | `refresh/` → `202 {task_id}` |
| `patient_portal/api/v1_urls.py` | `GET /derivation-status/{task_id}/` — same auth rule as `refresh/` |
| `ctomop/settings.py` | Celery block, and the production guard stops demanding `ALLOWED_HOSTS`/`CORS_ALLOWED_ORIGINS` of a worker, which serves no HTTP and otherwise cannot boot |

`202` instead of `200` + `derived_at` is a wire break: staging first,
healthkey-etl migrates, then production. Update `CLAUDE.md` and `API_SURFACE.md`.

## Config

`celery[redis]` and `redis` in `requirements.txt`.

| Env var | Default | Note |
|---|---|---|
| `CELERY_BROKER_URL` | `''` | The switch: empty → inline. Holds Redis auth — Secret Manager, never committed |
| `CELERY_RESULT_BACKEND` | same Redis | Result TTL bounds how long a caller can poll |
| `CELERY_WORKER_CONCURRENCY` | `4` | ×instances adds DB connections |
| `CELERY_WORKER_PREFETCH_MULTIPLIER` | `1` | Default 4 makes one worker hoard slow patients |
| `CELERY_TASK_TIME_LIMIT` | `900` | |
| `CELERY_BROKER_VISIBILITY_TIMEOUT` | `1800` | Feeds `broker_transport_options`. Must exceed the time limit or Redis redelivers a running task |
| `CELERY_RESULT_EXPIRES` | `86400` | After this an id reads `PENDING` again |

## Infrastructure

- **Render**: `type: redis` + `type: worker`, both in `render.yaml`. Fully
  specified, nothing left to decide.
- **Cloud Run**: unsettled, and the real work. None of it is declared in this
  repo — the staging service and its jobs are Terraform in the separate infra
  repo, and this repo only bumps their image tags. Three things have to be
  added there, and none exists yet:
  - a VPC and egress config. There is no network in that Terraform at all, so
    a private Memorystore is currently unreachable from any service in the
    family. A sibling service already hit this and fell back to running its
    async work inline.
  - the Redis instance itself.
  - a worker runtime. The worker cannot be a Cloud Run *service*: a revision
    only becomes ready once the container listens on `$PORT`, and Celery opens
    no socket. It needs a worker pool or another background runtime, with
    `--no-cpu-throttling` either way, since a throttled instance gets no CPU
    between polls and never consumes.

  `deploy-staging.yml` has no step for the worker — add one once the runtime is
  chosen, or it keeps running the image it was created with.

  Worth weighing before committing to any of that: the family's existing answer
  to async-on-Cloud-Run is Cloud Tasks, which needs no VPC and no broker. The
  dispatcher is a Protocol, so a Cloud Tasks implementation is a third class
  beside the Celery and inline ones rather than a rewrite.

## Testing

- **Task** — call `refresh_patient_record_task` directly, no broker. Patch
  `omop_core.services.patient_record_service.refresh_patient_record` (callers
  import it lazily) returning a real `PatientRecord` — a `MagicMock` makes DRF's
  encoder walk an infinite tree.
- **API** (fake dispatcher) — `refresh/` returns `202` + a task id and dispatches
  once; status endpoint reports success and failure; auth matches `refresh/`.
- **E2E** (`@pytest.mark.e2e`, own CI job with `redis:7-alpine` — the two
  existing suites already collide on `test_promop_test`) — real worker, POST,
  poll to `SUCCESS`, `derived_at` advanced.

## Rollout

The queued path has to run somewhere before it runs in production, and staging
is Cloud Run, which has no worker runtime yet. So the order is:

1. Merge. Nothing changes anywhere: no broker is set, so every deployment keeps
   deriving inline.
2. Settle the Cloud Run worker runtime (see Infrastructure), deploy Memorystore
   and a worker there, set `CELERY_BROKER_URL` on staging's web service.
3. Exercise it on staging: refresh the heaviest patient, poll to `SUCCESS`,
   force a failure and confirm it reports `FAILURE`.
4. healthkey-etl migrates from the `200` to polling the `202`.
5. Production: the Render blueprint already provisions Redis and the worker,
   but leaves `CELERY_BROKER_URL` unset on the web service on purpose — setting
   it in the dashboard is the flip, and clearing it is the rollback.

Until step 5 the web service still derives inline, so a rollback costs one
environment variable and no deploy.

## Follow-ups

Async hides the latency, it doesn't remove it.

- Incremental derivation — one new `Measurement` rebuilds all ~20 sections from
  full history.
- Bound `_build_snapshot` — it pulls every measurement and observation the
  person holds.
- The signal path and `_bulk_write` still derive inline; move them once this is
  proven.

## Definition of Done

- [ ] `refresh/` on the heaviest known patient answers in well under a second
      and never 500s on a slow derivation.
- [ ] Polling the returned task id reaches `SUCCESS`, and
      `PatientRecord.derived_at` advanced.
- [ ] A derivation that fails is reported as `FAILURE` with a readable error,
      not as a success over a stale record.
- [ ] healthkey-etl can refresh its whole patient set through the queue with DB
      connections within cap.
- [ ] A developer with no Redis running still gets a working refresh.
- [ ] Both existing backend suites still green.
- [ ] `CLAUDE.md` and `API_SURFACE.md` describe the `202` contract well enough
      to write a client against.
