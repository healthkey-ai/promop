# Render staging Celery

Render staging is `promop-staging` at https://promop-staging.onrender.com, on Render
in Oregon, tracking `dev`. Local staging database access uses
`STAGING_DATABASE_URL` from `.env`. Render processes use `DATABASE_URL` for
that same existing database. Cloud Run staging has a separate deployment configuration.

`render.yaml` now contains the concrete staging web, worker, and private
Redis-compatible Key Value definitions alongside the existing production
services. No generated file is needed. Staging does not create a new database
or reference the production database.

## Environment settings

The Blueprint sets staging's allowed host, CORS origin, and application URL,
and explicitly points both web and worker at `promop-staging-redis` for
`CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND`.

`DATABASE_URL` stays dashboard-managed on the existing staging web service;
its value is the staging database connection string stored locally under
`STAGING_DATABASE_URL`. The worker references this web environment variable.
Database credentials must not be committed to the public repository.

The worker also references the web service's `SECRET_KEY`, `AUDIT_HMAC_KEY`,
`EXPORT_SIGNING_KEY`, and `ANTHROPIC_API_KEY`. Render's `generateValue: true`
preserves existing signing keys and supplies keys only if absent. The ranking
API key and other external-service credentials stay dashboard-managed. Render
ignores newly added `sync: false` entries during an existing Blueprint update;
if a required external credential is absent, it must be set in Render first.

## Apply

Sync `render.yaml` from `dev` in the Blueprint managing these services. An
ordinary Git-triggered code deploy does **not** create worker/broker resources
or apply Blueprint environment settings. If staging is not Blueprint-managed,
associate the existing service when creating the Blueprint. Do not attach a
service already managed by one Blueprint to a second Blueprint.

The worker uses a 2 GB plan (`1c-2g`) with concurrency 1 and prefetch 1. This
avoids four processes independently loading the embedding model; it is not a
diagnosis of the reported memory failure. The broker is 256 MB with
`noeviction`, so memory pressure cannot silently evict queued jobs. These are
additional paid Render resources. Measure memory before raising concurrency.

If using the Dashboard instead, create the worker and broker with the settings
in `render.yaml`, supply the same database and secret values to the worker,
and set the web service's broker and result backend to the broker's internal
connection string. Preserve any existing explicit `CACHE_URL`. Otherwise,
Django uses the broker for its shared cache.

## Verify

- Worker logs must show a connection to the staging broker and registration of
  `omop_core.suggest_mappings`. From the Render shell,
  `celery -A promop inspect ping` must get a worker response.
- Authenticated `/api/v1/code-mappings/reference/` must return
  `suggest_max_per_run: 100`.
- Run Suggest on a curator-approved queue. The request must return 202 promptly,
  the worker must receive the job, and its progress must reach success. Seeing
  100 in the UI proves broker configuration, not worker health.
- Check web and worker memory during the run. Do not run the test suite against
  staging.

To roll back, remove staging's broker/result-backend references from the
Blueprint and clear those web environment values in Render, then redeploy.
This restores synchronous processing with the three-code limit. Drain running
jobs before stopping the worker; retain Redis until queued work is accounted
for so a later sync cannot discard pending work.

Local tests use isolated PostgreSQL, Redis, and a real Celery worker to verify
100-code Suggest runs and patient derivation without external model API calls.

Render reference: https://render.com/docs/blueprint-spec


## Security configuration

`DEBUG` does not select security defaults. Render web and worker processes are
recognized through Render's environment markers, including `RENDER=true` on
workers. From the **web** service shell, run:

```bash
python manage.py check --deploy --fail-level ERROR
```

`patient_portal.I001` reports effective security controls without printing
credentials or identity-service URLs. See [security settings](security-settings.md)
for explicit overrides and local HTTP setup. Build commands retain their startup
exemption; `check --deploy` validates runtime configuration before migrations.

Google Cloud Run staging is a separate supported deployment, maintained by
[its workflow](../.github/workflows/deploy-staging.yml) and
[Dockerfile.gcp](../Dockerfile.gcp). Its existing service/image/bucket identifiers
remain unchanged. The Render database connection documented here must not be
assumed to identify the Cloud Run database.
