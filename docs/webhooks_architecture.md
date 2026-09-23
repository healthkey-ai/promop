# Webhook notifications

PRomop receives signed external notifications and sends organization-scoped
patient-change notifications. These messages carry identifiers, not lab values,
document contents, or patient demographics. Inbound events notify subscribers;
they do not import or modify clinical data. Use the existing lab sync, document,
or FHIR APIs for authenticated clinical writes.

`WEBHOOKS_ENABLED=true` enables publishing, inbound processing, and delivery. It
has to be true on the web service **and** its worker; on Render, set it on the
web service only — `render.yaml` declares it there and each worker pulls that
exact value, so there is one place to flip and no way for the two to disagree.
Setting it on a worker directly fights that reference. It defaults to false: clinical signal/bulk hooks return
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
JSON/schema or a patient outside the source organization returns 400. The
patient rule governs a first-time event id only: a replay of an event this
endpoint already accepted answers `duplicate` even if that patient has since
been deleted or moved to another organization, because the sender is retrying
an answer it missed, not asserting anything new.

A relayed event carries `origin`: `{"source": ..., "event_id": ...}`, naming the
inbound source and its event id. Events this service originates have no such key.
Each hop mints a fresh event id, so `(source, event_id)` deduplication cannot see
a cycle: an organization subscribed to the partner that feeds it gets its own
event back. The marker lets that partner recognise its own traffic and stop —
subscribers should drop an event whose `origin.source` names them. It does not
constrain a partner that ignores it, so the inbound quota remains the hard bound.

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
then retry with a fresh signature/timestamp and the same ID/body. A per-IP ingress bucket
(`WEBHOOK_INGRESS_RATE`, default 1,200/minute) also applies, in front of
`WEBHOOK_TRUSTED_PROXY_DEPTH` is what makes that meter unforgeable: the client address is taken that many entries from the right of `X-Forwarded-For`, so a value larger than the number of proxies actually in front of the deployment lets a caller choose its own bucket. It is 1 on Render; use 0 when nothing fronts the application.
signature verification, so traffic that never verifies is still metered. It is
deliberately set above the per-source quota so a correctly signed sender always
meets its own limit first; the project's generic 60/minute anonymous bucket is
not used here, because this endpoint authenticates by signature rather than by
session and every caller would otherwise count as anonymous. Use a shared Django cache across
web instances for a shared quota; DRF cache throttling is approximate under
concurrency.

## Subscriptions

Authenticated staff or organization administrators may register a subscription:

```http
POST /api/v1/webhooks/subscriptions/
Content-Type: application/json

{"organization":42,"url":"https://subscriber.example/events","event_types":["patient.changed","lab.updated"],"active":true}
```

Read and write authorities differ here, on purpose. Reading subscriptions and
their delivery history follows `get_admin_orgs`, which is wider than a direct
grant: platform staff see every organization, a live `org_admin` grant sees its
own, and a non-patient professional role reaches further organizations through
organization and domain trusts — the same reach that decides whose patients they
work with, so the list matches the data they already see. Creating, editing or
deleting a subscription follows `get_direct_admin_orgs`: platform staff and
direct `org_admin` grants only. A trust is granted for data access, and naming
where an organization's events are sent is data-egress configuration, so a trust
does not carry it ([decision](soc2/webhook-egress-authority.md)). Writes also
require an interactive session — a session or a partner (Firebase/PHR) token —
because creating a subscription mints a long-lived signing secret and names
where an organization's data goes, which is credential administration and must
require the person rather than something they handed out. So an OAuth access
token or a service credential **cannot create, edit or delete a subscription at
any scope**; it can read, and reads still need the relevant scope. Partner
tokens carry no scopes at all, so for them the two sets above — wide for reads,
direct grants only for writes — are the whole gate; session callers are covered by CSRF enforcement on the endpoint. The 201 response includes
the generated `secret` once: store it at the subscriber. List, detail, update,
and delivery-log responses never expose the secret. Subscriptions cannot be
transferred between organizations. `PATCH /api/v1/webhooks/subscriptions/{id}/`
can update URL/event types or disable with `{"active":false}`; DELETE marks the
subscription removed. Rotation is done by creating a new subscription and
disabling/deleting the old one.

### Every change to a destination is recorded

A subscription names where an organization's patient events go, so changing one
is the act worth recording — and the generic audit row carries method, path and
status, which says that an egress configuration changed but not what it became.
`POST`, `PATCH` and `DELETE` therefore each append a `webhook_subscription_change`
row: acting identity and email, organization (id and slug), subscription id, the
URL and event types on both sides of the change, and the timestamp. The rows are
append-only — `save()` on an existing row and `delete()` both refuse — and
retention never prunes them. `QuerySet.update()` and direct SQL are not stopped
by the model; a database role that cannot write the table is what makes that
durable, and it belongs to the deployment.

A change made at a shell, by a data migration or through a fixture writes no
record. That is the same limit the model-level URL validator has, for the same
reason: the field this table exists for is the actor, and a signal cannot name
one. The same facts also go onto the request's `AuditEvent` row, whose
`detail` is covered by that table's HMAC signature and hash chain — so the
change log is the queryable index and the audit chain is the tamper-evidence,
and rewriting one contradicts the other. The audit row carries the destination
**host and a SHA-256 digest of the URL**, never the URL: audit rows go to stdout
for the SIEM and are readable through `/api/v1/audit-events/` by platform staff
and any service token, which is a wider audience than the direct org admins who
may configure egress, and for some receivers the path is the credential. The
digest still binds the exact address.

`DELETE` marks `deleted_at` and clears `active` rather than deleting the row.
The cascade used to take the subscription's delivery history with it, so after
removing a subscription nothing said where that organization's events had been
going — which is the one question an investigation asks. A removed subscription
accepts no write (redirect, re-enable or a second delete all 404) and receives no
further events, and it leaves the listing — removing a subscription still means
it stops appearing, which is the contract clients were written against.
Retrieving it by id keeps working, and so does its delivery history;
`?include_removed=true` brings removed subscriptions back into the listing for
whoever is reviewing that history.

Each delivery freezes the address it is for when the row is written, and the
worker sends there rather than to wherever the subscription points at send time.
A row can sit through minutes of retry backoff and a recovery sweep, so reading
the destination at send meant a `PATCH` both redirected PHI that was already
queued and left any record of where it went written after the fact. Frozen, a
redirect governs future events only, every attempt on a row went to the same
place, and the row can say so. The delivery API exposes the `destination_host`
derived from it, never the address: for some receivers the path is the
credential. Changing the URL therefore cancels what was
already queued against the old address rather than forwarding it: nothing goes
to an address the organization has stopped designating, and nothing already
queued is redirected to the new one. That is what makes a URL change a working
kill switch — without it, a subscription disabled over a bad destination would
flush its backlog there as soon as it was re-enabled. Deliveries cancelled this
way are not re-sent; re-publish if they matter. Cancelling at the moment of the
change cannot reach every row — a publisher that read the subscription just
before it still inserts one addressed to the old URL, and a row a worker is
holding has passed the point — so every attempt also compares the address it
was frozen for against the one the organization designates now, and stops if
they differ. That check and the attempt's claim commit together, which fixes
the boundary: a change stops everything that has not yet begun an attempt, and
an attempt already under way completes and is recorded. Nothing can recall a
request in flight, and that one was addressed to what the organization
designated when it started. A row written before the address column existed carries none and
follows the subscription, which is all it can do.

Deliveries do not survive deleting the organization — that cascade still reaches
them. The change rows do, which after an organization is removed makes them the
only remaining trail.

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

A cross-instance patient copy (`copy_patient`) is a bulk write of every one of
a patient's tables at once, so it announces itself the same way, one aggregate
per table rather than one event per row — the count is what changes with the
size of the patient, not the number of events. `--replace` deletes the rows
that patient had here before writing the new ones under new ids, and both
halves are announced: `bulk_deleted` with the previous count, then `bulk_saved`
with the new one. The copy lands under a person_id of this database, which need
not be the one the deleted rows had, and the receiving organization need not be
the one that held them — each deletion is addressed to the organization that
actually had those rows, under the person_id it knew them by. A subscriber
mirroring rows by id should treat the pair as "drop what you have for that
person_id, then re-fetch".

The outbox insert is deliberately part of the writer's transaction and is not
wrapped in a try/except: that is what makes "the row exists" and "a
notification for it exists" one fact rather than two. The cost is real and
should be understood before enabling this in production — with
`WEBHOOKS_ENABLED=true`, a failure to insert the outbox row (lock contention,
a statement timeout, webhook tables missing on a lagging replica) rolls back
the clinical write that triggered it, and each signalled row costs one
subscription lookup plus one insert. Swallowing those errors would trade a
visible failure for silent, permanent notification loss, which is the worse
side for an audit-relevant egress path.

Delivery bodies contain `id`, `type`, `occurred_at`, and `data` with identifiers
and operation metadata, plus `origin` on a relayed event (see Inbound above) —
so a subscriber validating against a strict schema should allow it rather than
reject the delivery. `origin.source` is the inbound source id the feeding
partner sends in `X-HealthKey-Source`, visible to every subscriber of that
organization; both sides are configured by the same org admin. Outbound `X-HealthKey-Signature` uses the subscription
secret over `<X-HealthKey-Timestamp>.<raw body>`, the same construction the
inbound endpoint verifies, so a subscriber can and should reject deliveries
whose timestamp is outside its own tolerance — five minutes is what this
service uses inbound. `X-HealthKey-Delivery` is a stable delivery UUID and is
**not** covered by the signature, so treat it as a retry hint, not as
authentication.

> **Signature format change.** Outbound deliveries were originally signed over
> the body alone. Any subscriber provisioned against that construction must be
> updated to verify `<timestamp>.<body>` before this is enabled for it — the
> old form no longer verifies. The feature ships disabled
> (`WEBHOOKS_ENABLED=false`), so there is no live subscriber to migrate today;
> this note exists so nobody provisions one against the wrong scheme.
 Consumers should
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

Both beat tasks return immediately unless `WEBHOOKS_ENABLED` is true, so the
scheduler is only as useful as that flag: a web service publishing events while
its worker has the flag off leaves every delivery `pending` with no error
anywhere. `render.yaml` therefore declares it on the web service and pulls the worker's
value `fromService`, the mechanism `SENTRY_DSN` already uses between these two
services, and `test_render_staging_blueprint.py` asserts the pair. (Not the
broker URL's mechanism: each service points independently at the Redis resource.
`SECRET_KEY` and the broker URL are set-once values that converge by
construction; this flag is the first `fromService` reference whose purpose is to
be toggled later, so it converges only once the worker redeploys — which is what
"restart web and workers after changing this flag" above means in practice.)

**Applying this to an existing Render deployment:** a newly added `sync: false`
entry is ignored on a Blueprint update of a service that already exists (see
[Render configuration](render-staging-celery.md)), so add `WEBHOOKS_ENABLED` to
the web service in the Render dashboard first, then sync the blueprint — the
worker's reference is applied normally and would otherwise point at a variable
that does not exist yet.

`start-worker.sh` starts embedded Celery beat when `CELERY_EMBEDDED_BEAT=true`,
with its schedule in `/tmp/promop-celerybeat-schedule`. It is off unless asked
for, because beat is a singleton and the default must stay safe when a worker
service is scaled to more than one replica; `render.yaml` sets it on both
single-replica workers — staging and production. Production previously started
Celery directly rather than through `start-worker.sh`, so the flag was
unreachable there and neither the recovery sweep nor retention ran; the
blueprint test asserts the entrypoint now. Every minute it queues up to 1,000 due outbox
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
timestamps, and the `destination_host` the row is addressed to. Response bodies,
full destination URLs, secrets, and notification payloads are excluded — the
host, not the path, because for some receivers the path is the credential. The
address is frozen, so every attempt on a row went to that host; `attempts` is
what says whether anything was sent there at all. Reading the table as "where
events went" means reading the rows with attempts, not every row. Dead letters remain available for investigation; there
is no automatic reset of exhausted attempts.

Migrations `patient_portal.0021`, `0022` and `0023` add four tables, a uniqueness
constraint, a partial index for active deliveries, and the change log with the
`deleted_at`/`destination_url` columns;
it does not modify existing clinical rows. Apply migrations before web/worker
deployment. Treat signing secrets and delivery records as protected application
data when configuring database access, backups, and retention.

## Retention

`CELERY_BEAT_SCHEDULE` runs `patient_portal.tasks.prune_webhook_history` daily at
03:30 UTC (`CELERY_TIMEZONE` is unset, so celery's UTC default applies whatever
Django's `TIME_ZONE` says), which calls the command below; the deployment that runs beat therefore
runs retention too, and there is no second thing to keep configured. A fixed hour rather than an
interval, because a redeploy resets beat's schedule file in `/tmp`: with
redeploys more frequent than the interval, an interval-scheduled pass would
never fire at all. The trade is that a redeploy spanning 03:30 skips that day
rather than running late, which retention can afford. Run it by hand the same way on a
deployment without beat: `python manage.py prune_webhooks` on a process with the
same configured database. Default retention is 30 days (`WEBHOOK_RETENTION_DAYS`);
`--days N`, `--batch-size N` (default 1000), and `--dry-run` are supported.
Retention must be at least one day, beyond the five-minute signature window.
The command removes old terminal delivery history (`delivered`, `dead_letter`,
`cancelled`) and old processed inbound deduplication rows. It does not touch
`webhook_subscription_change`: that is the record of who pointed an
organization's events where, and it is kept rather than aged out. Active deliveries,
unprocessed inbound events, and recent completions remain. Archive history
externally before pruning if longer retention is needed. An authorized sender
can reuse an expired event ID with a newly signed request after retention ends;
use unique event IDs. The recovery sweep uses an index limited to active statuses
and queues the oldest due rows first.
