# Webhook notifications

PRomop receives signed external notifications and sends organization-scoped
patient-change notifications. These messages carry identifiers, not lab values,
document contents, or patient demographics. Inbound events notify subscribers;
they do not import or modify clinical data. Use the existing lab sync, document,
or FHIR APIs for authenticated clinical writes.

Set `WEBHOOKS_ENABLED=true` on web and workers to enable publishing, inbound
processing, and delivery. It defaults to false: clinical signal/bulk hooks return
before any webhook database queries, inbound returns 503, and delivery tasks pause
without changing queued rows. Subscription management stays available for setup.
Restart web and workers after changing this flag.

## Inbound

Configure `WEBHOOK_INBOUND_SOURCES` on the web service as a JSON object:

```json
{"ehr-labs":{"organization":"your-org-slug","secret":"a-separate-random-secret"}}
```

Each source has a distinct secret and one organization chosen by the operator.
Never use a Django, audit, OAuth, or outbound subscription secret here.
An empty configuration disables inbound access. Send requests to
`POST /api/v1/webhooks/inbound/` with `Content-Type: application/json`,
`X-HealthKey-Source: ehr-labs`, `X-HealthKey-Timestamp: <Unix seconds>`, and
`X-HealthKey-Signature: sha256=<hex digest>`. Compute HMAC-SHA256 over
`ASCII(timestamp) + b'.' + exact_request_body_bytes` with the source secret.
The shared Python helper is
`patient_portal.webhooks.compute_hmac_signature(payload_bytes, secret, timestamp)`.
The timestamp must be within 300 seconds of server time, including future clock
skew; missing, malformed, expired, or modified timestamps return 401. Keep source
clocks synchronized and sign each retry with a fresh timestamp and the same event
ID/body. Captured old signatures cannot be replayed after deduplication rows expire.

```json
{"id":"external-event-123","type":"lab.updated","data":{"person_id":42,"resource_id":"external-lab-456"}}
```

Supported inbound types are `lab.updated`, `document.received`, and
`foundation.synced`. Each handler creates outbound delivery records for its
matching active subscriptions and marks the inbound event processed in the same
transaction. `data.person_id` must belong to the configured organization;
`data.resource_id` is optional. Additional input fields are ignored and are not
forwarded. Bodies are limited to 64 KiB. Invalid signatures return 401, invalid
JSON/schema or a patient outside the source organization returns 400.

The signed `id` is the idempotency key, unique per source (maximum 128 characters).
An optional `Idempotency-Key` header must equal it. First processing returns 202;
a retry within the retention period returns 200 with `duplicate: true`. Reusing an ID with different
body bytes returns 409. Unique database constraints also deduplicate simultaneous
requests; a rolled-back handler can be retried. Records store a SHA256 digest,
source, event ID, type, organization, and timestamps, not the original body.

Authenticated inbound requests share a per-source quota, default 600/minute,
configured with `WEBHOOK_INBOUND_RATE` (DRF rate syntax, e.g. `1200/minute`).
Different sources behind one IP have separate quotas. Invalid signatures do not
consume a source quota. A 429 includes `Retry-After`; wait that many seconds,
then retry with a fresh signature/timestamp and the same ID/body. The generic
anonymous 60/minute throttle does not apply. Use a shared Django cache across
web instances for a shared quota; DRF cache throttling is approximate under
concurrency.

## Subscriptions

Authenticated staff or organization administrators may register a subscription:

```http
POST /api/v1/webhooks/subscriptions/
Content-Type: application/json

{"organization":42,"url":"https://subscriber.example/events","event_types":["patient.changed","lab.updated"],"active":true}
```

Organization access follows the existing administrator grants and trusts. OAuth
tokens must also hold the relevant read/write scope. The 201 response includes
the generated `secret` once: store it at the subscriber. List, detail, update,
and delivery-log responses never expose the secret. Subscriptions cannot be
transferred between organizations. `PATCH /api/v1/webhooks/subscriptions/{id}/`
can update URL/event types or disable with `{"active":false}`; DELETE removes
the subscription and its delivery history. Rotation is done by creating a new
subscription and disabling/deleting the old one.

Outbound types are `patient.changed`, `lab.updated`, `document.received`, and
`foundation.synced`. Ordinary saves/deletes of patient records, demographics,
clinical rows, documents, trial enrollments, and oncology episodes create events
for the owning organization. Measurement saves use `lab.updated`; document
saves use `document.received`; deletions use `patient.changed`. Primary FHIR,
lab-sync, wearable, and generic clinical bulk API writes explicitly publish one
notification per changed patient/table batch, including bulk updates/deletes
with `skip_refresh=true`. New code using `bulk_create`,
`bulk_update`, or `QuerySet.update` must call `publish_patient_bulk_change` inside
its write transaction because Django does not emit model signals for these.
Unassigned patients do not produce tenant notifications. A clinical save may
also refresh PatientRecord and consequently emit a separate `patient.changed`.

Delivery bodies contain `id`, `type`, `occurred_at`, and `data` with identifiers
and operation metadata. Outbound `X-HealthKey-Signature` uses the subscription
secret, and `X-HealthKey-Delivery` is a stable delivery UUID. Consumers should
deduplicate this UUID: delivery is at least once, including when a subscriber
accepts a request but its response is lost. Only public HTTPS URLs on port 443
are allowed for delivery. Subscription creation/update validates URL syntax and
rejects unsafe literal IPs without resolving DNS. A hostname may be accepted at
registration yet fail delivery validation. DNS is resolved and all returned
addresses are checked inside the worker for every attempt; connections are pinned to a
validated public address with hostname-verified TLS. Redirects are not followed.

## Delivery operations

Outbox rows are persisted with the clinical transaction, and broker publishing
runs after commit, with Celery publish retries disabled, no result-backend write,
and 2-second broker connect/socket timeouts. Broker failure leaves rows for the
recovery sweep. A rollback produces no delivery for that transaction. FHIR bundle
uploads use one transaction per patient: a failed patient's writes and outbox rows
roll back together, while successful patients can commit and notify even when
another patient fails (the response reports partial success). Configure the same database
and Celery broker on web and worker. With no broker configured, rows remain
pending; no network request runs inside the web process.

The worker makes at most five attempts. Any non-2xx response (including 4xx/5xx),
network error, or unsafe destination retries after 30, 60, 120, then 240 seconds.
The fifth failure becomes `dead_letter`. Disabling a subscription cancels queued
deliveries when processed; an already-running HTTP request may finish. A worker
claims each attempt under a database lock before sending, preventing duplicate
Celery messages from sending concurrently. HTTP sends have 5-second connect and
10-second read timeouts; this task alone has 45/60-second soft/hard limits and a
120-second recovery lease. Lost worker attempts count toward the same limit.

`start-worker.sh` starts embedded Celery beat by default, with its schedule in
`/tmp/promop-celerybeat-schedule`. Every minute it queues up to 1,000 due outbox
rows, recovering broker outages, lost tasks, and expired leases. The existing
Render `promop-staging-worker` is a single worker service; staging is
https://promop-staging.onrender.com (see [Render configuration](render-staging-celery.md)).
Run exactly one beat scheduler per broker/database deployment. Before adding
worker replicas, set `CELERY_EMBEDDED_BEAT=false` on **all** workers and run one
separate `celery -A promop beat --schedule=/tmp/promop-celerybeat-schedule`
process with the same configuration. Disabling embedded beat without a separate
scheduler disables automatic recovery sweeps, though normal dispatch and retries
still work. For a manual sweep, run:

```sh
python manage.py shell -c 'from patient_portal.tasks import dispatch_pending_webhooks; dispatch_pending_webhooks()'
```

`GET /api/v1/webhooks/subscriptions/{id}/deliveries/` returns authorized delivery
history with status, attempts, next attempt, HTTP status, a redacted error code,
and timestamps. Response bodies, destination URLs, secrets, and notification
payloads are excluded. Dead letters remain available for investigation; there
is no automatic reset of exhausted attempts.

Migrations `patient_portal.0018` and `0019` add three tables, a uniqueness constraint,
and a partial index for active deliveries;
it does not modify existing clinical rows. Apply migrations before web/worker
deployment. Treat signing secrets and delivery records as protected application
data when configuring database access, backups, and retention.

## Retention

Schedule `python manage.py prune_webhooks` daily on a process with the same
configured database. Default retention is 30 days (`WEBHOOK_RETENTION_DAYS`);
`--days N`, `--batch-size N` (default 1000), and `--dry-run` are supported.
Retention must be at least one day, beyond the five-minute signature window.
The command removes old terminal delivery history (`delivered`, `dead_letter`,
`cancelled`) and old processed inbound deduplication rows. Active deliveries,
unprocessed inbound events, and recent completions remain. Archive history
externally before pruning if longer retention is needed. An authorized sender
can reuse an expired event ID with a newly signed request after retention ends;
use unique event IDs. The recovery sweep uses an index limited to active statuses
and queues the oldest due rows first.
