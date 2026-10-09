# Error monitoring (Sentry)

Sentry is initialised in `promop/settings.py` through `promop/sentry.py`, and
only when `SENTRY_DSN` is set. Local development, CI and the test suite leave it
unset, so nothing is sent and no vendor account is needed to run the project.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SENTRY_DSN` | *(unset)* | The project DSN. Unset disables the SDK entirely — that is also the rollback. |
| `SENTRY_ENVIRONMENT` | `development` when `DEBUG`, else `production` | The environment tag on every event. |
| `SENTRY_RELEASE` | `RENDER_GIT_COMMIT`, else none | Groups an error by the code that raised it. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0.0` | Performance tracing. Off, because errors are what this is for. |
| `SENTRY_PROFILE_SESSION_SAMPLE_RATE` | `0.0` | Profiling. |
| `SENTRY_SEND_DEFAULT_PII` | `false` | See below. Leave off. |

On Render, `SENTRY_DSN` is dashboard-managed (`sync: false`) on both web
services; each worker inherits the DSN from its web service, so a task and the
request that queued it land in one project.

## What reports an error

| Path | Reported by |
|---|---|
| An exception escaping a view | `DjangoIntegration` (via `got_request_exception`) |
| A DRF `APIException` answered as 5xx | `patient_portal.api.exception_handlers.sentry_exception_handler` |
| A 5xx a view returned without raising | `SentryServerErrorMiddleware` |
| A Celery task failure | `CeleryIntegration` |
| Any `ERROR` log record | `LoggingIntegration(event_level='ERROR')` |

The DRF handler exists because DRF never lets an `APIException` escape: it turns
one into a response, so without it a 500 built by DRF is as silent as a 400.
Client errors are not reported — a 400 or a 403 is the API working. An exception
DRF does not recognise is left to Django, which reports it once.

A view that returns `Response(status=503)` raises nothing, so neither of those
sees it. `SentryServerErrorMiddleware` reports it as a message, skipping the
responses DRF and Django already marked as exception output.

Django logs every 5xx response on `django.request`, which the logging
integration would otherwise turn into a second event for the same failure.
`DjangoIntegration` calls `ignore_logger('django.request')` for exactly that
reason, so each failure is one event. `TestOneEventPerFailure` holds that.

## PHI

This service stores patient data, so the SDK's capture defaults are narrowed in
`promop/sentry.py`:

- `send_default_pii=False` — no signed-in user, no cookies, no `Authorization`
  header.
- `max_request_body_size='never'` — request bodies are FHIR bundles and OMOP
  rows.
- `include_local_variables=False` — a frame that parses a bundle holds the whole
  bundle in its locals.
- `before_send` drops the query string, replaces the id segments of the request
  path with `:id`, redacts email addresses out of log messages, and filters
  connection strings out of a management command's `argv`. It is registered for
  transactions too, which skip `before_send`.

An `ERROR` log record becomes an event with the message the caller formatted, so
a logger that interpolates patient data sends it. Email addresses are redacted
defensively, but the general fix is the log call: pass an id the event can be
scrubbed by, not a name, an address or a source value.
- The event scrubber's denylist is extended with this deployment's own secrets
  (`AUDIT_HMAC_KEY`, `EXPORT_SIGNING_KEY`, `SERVICE_AUTH_TOKEN`, the database
  URLs, and the rest).

Turning `SENTRY_SEND_DEFAULT_PII` on sends identifying data to a third party and
needs a DPA that covers it first — see
`docs/promop-security-soc2-remediation-plan.md`.

## Checking it works

With a DSN configured, raise one deliberately:

```bash
.venv/bin/python manage.py shell -c "import sentry_sdk; sentry_sdk.capture_message('promop smoke test')"
```
