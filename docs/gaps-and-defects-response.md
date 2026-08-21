# PRomop Response to Gaps & Defects Assessment

Response to [promop-gaps-and-defects.md](https://github.com/HealthTree/one/blob/hk/phr-to-promop/docs/promop-gaps-and-defects.md),
originally verified against `promop@main` at tag `v1.1.0` (2026-08-20).
Status below reflects fixes merged to `promop@dev` as of 2026-08-21.

This document intentionally lives on `dev`: the fixes it references have been
merged to `dev` and are not all present on `main`.

---

## Status Summary

| #   | Item                                      | Status             | Current dev status                                      |
| --- | ----------------------------------------- | ------------------ | ------------------------------------------------------- |
| D0  | Container can't start with `DEBUG=False`  | **Fixed on dev**   | #554 closed                                             |
| D1  | Patient can't write own clinical data     | **Fixed on dev**   | #555 closed                                             |
| D2  | Provenance headers fail CORS preflight    | **Fixed on dev**   | #556 closed                                             |
| D3  | `find_or_create` mints duplicate Person   | **Fixed on dev**   | #557 closed                                             |
| D4  | Second PATCH by same actor 500s           | **Fixed on dev**   | #558 closed                                             |
| D5  | Partner-auth requests not audited         | **Fixed on dev**   | #559 closed                                             |
| D6  | Read/write access disagree at org scope   | **Fixed on dev**   | #560 closed                                             |
| D7  | FHIR dedup on display text                | **Fixed on dev**   | #561 closed                                             |
| D8  | FHIR ingest drops unmapped fields         | **Fixed on dev**   | #562 closed                                             |
| G1  | Clinical reads unpaginated                | **Fixed on dev**   | #564 closed; pagination is opt-in for compatibility     |
| G2  | Unknown filters silently ignored          | **Fixed on dev**   | #563 closed                                             |
| G3  | Only `person_id` filterable               | **Fixed on dev**   | #565 closed                                             |
| G4  | FHIR ingest covers only 4 resource types  | **Fixed on dev**   | #569 adds DocumentReference; prior dev already expanded |
| G5  | No v1 `/me/` endpoint                     | **Fixed on dev**   | #566 closed                                             |
| G6  | Single-row create has no idempotency      | **Fixed on dev**   | #567 closed                                             |
| G7  | Service token is one shared secret        | **Open**           | #568 remains open as Medium priority                    |
| G8  | Partial FHIR ingest undetectable          | **Fixed on dev**   | #570 closed                                             |

---

## Detailed Responses

### D0 — Container can't start with `DEBUG=False`

**Status: Fixed on dev (#554).**

The report is accurate: `docker-compose.yml` does not pass `ALLOWED_HOSTS` or
`CORS_ALLOWED_ORIGINS`, and the `command:` block has a YAML folded-scalar
formatting issue.

**Context:** PRomop does not deploy via Docker Compose. Production runs on
Render (backend) and GCP Cloud Run (Module Federation remote), both using their
own Dockerfiles (`Dockerfile` and `Dockerfile.gcp`) with env vars injected by
the platform. `docker-compose.yml` exists for local development convenience
with `DEBUG=True`.

**Current status:** The Docker Compose `DEBUG=False` startup path was fixed on
`dev`. This remains primarily a local integration-testing path; production
continues to run through Render and GCP Cloud Run.

---

### D1 — Patient can't write own clinical data

**Status: Fixed on dev (#555).**

`ScopedTokenPermission` on the five clinical viewsets grants unsafe methods
(POST, PUT, DELETE) only to `is_staff` or service-token callers. This blocks
patients and non-staff clinicians from creating clinical rows via the typed
OMOP endpoints.

**Important caveat from the report itself:** `POST /api/v1/fhir/patient-sync/`
uses `IsAuthenticated` and already accepts a patient's own token. The FHIR
sync endpoint is the primary write path for the PHR migration. The typed
endpoints (measurements, observations, etc.) are used for programmatic OMOP
writes, which is a secondary path.

**Current status:** Typed clinical write endpoints now use row-level patient
authorization through `PatientCrudPermission` and ownership checks. The fix was
kept scoped to endpoints that can establish patient ownership safely.

---

### D2 — Provenance headers fail CORS preflight

**Status: Fixed on dev (#556).**

`X-Provenance-Source` and `X-Provenance-User-Id` are not in
`CORS_ALLOW_HEADERS`. The fix is adding them to the setting. This is
straightforward.

**Current status:** Provenance request headers are allowed through CORS
preflight.

---

### D3 — `find_or_create` mints duplicate Person

**Status: Fixed on dev (#557).**

The report correctly identifies that `find_or_create` looks up by
`actor_iss`/`actor_sub` while the patient portal resolves via `PatientUser`.
When a patient has signed in through the portal (creating a `PatientUser` link
but not populating `actor_iss`/`actor_sub`), a staff-side
`find_or_create` call creates a second `Person`.

**Current status:** `find_or_create` resolves existing `PatientUser` links
before minting a new `Person`, converging portal and staff-side resolution.

---

### D4 — Second PATCH by same actor 500s

**Status: Fixed on dev (#558).**

`_record_provenance` uses `ProvenanceRecord.objects.create()`, which violates
the unique constraint `(content_type, object_id, source_user_id, source)` on a
second write by the same actor.

**Current status:** Provenance recording is idempotent for repeated edits by
the same actor.

---

### D5 — Partner-authenticated requests not audited

**Status: Fixed on dev (#559).**

`_get_client_id` falls back to `str(token)`, which for a `TokenClaims` object
can exceed the 255-character `client_id` column, causing a silent `DataError`.
The insert failure is caught and logged as a warning, but the audit row is
lost.

**Current status:** Partner-token audit attribution stores bounded issuer and
subject data instead of `str(request.auth)`.

---

### D6 — Read and write access disagree at org scope

**Status: Fixed on dev (#560).**

`can_access_patient` checks only `group__memberships__person_id`.
`can_write_patient` additionally checks `org__patients__person_id`. An
org-granted clinician can write data they cannot read back.

**Current status:** Org-scope patient access predicates were unified across
read, write, and actor-role paths, with role filtering retained.

---

### D7 — FHIR dedup on display text

**Status: Fixed on dev (#561).**

The dedup key in `_upsert_clinical` uses `source_value` (populated from FHIR
`display` text) rather than the resolved `concept_id`. This is documented and
intentional: it allows vocabulary loads to upgrade a stored row's concept
in-place without the row's identity changing.

However, the report's concern is real: two producers spelling the same coded
fact differently (e.g., "Hemoglobin" vs "Hemoglobin [Mass/volume] in Blood")
will create duplicate rows even though PRomop resolves both to the same OMOP
concept. At the cross-producer seam this is a data correctness issue.

**Current status:** FHIR Observation ingest deduplicates mapped rows by
resolved concept rather than display text.

---

### D8 — FHIR ingest drops unmapped fields

**Status: Fixed on dev (#562).**

`referenceRange` maps to OMOP `Measurement.range_low` / `range_high`.
`interpretation` maps to `value_as_concept`. Both have OMOP columns; both are
silently dropped during ingest. The report is correct that this is silent data
loss, especially problematic for a migration that intends to retire the source
copy.

**Current status:** FHIR Observation ingest maps OMOP-backed reference ranges
and interpretation fields.

---

### G1 — Clinical reads are unpaginated

**Status: Fixed on dev (#564).**

No `pagination_class` on any of the five clinical viewsets, and no
`DEFAULT_PAGINATION_CLASS` in `REST_FRAMEWORK` settings. A bulk-loaded patient
returns their entire clinical history in one response.

**Current status:** Clinical OMOP list endpoints support opt-in pagination via
`page`, `page_size`, and `limit`. Existing clients that omit pagination
parameters still receive the legacy bare-list response shape.

---

### G2 — Unknown query filters silently ignored

**Status: Fixed on dev (#563).**

The report is right that this is the most insidious item: it produces a
**wrong answer** instead of a failure. A caller that asks for
`?observation_date=2026-01-01` gets the whole history and has no way to know
the filter was ignored.

**Current status:** Clinical list endpoints reject unsupported query
parameters with HTTP 400 and disclose supported parameters.

---

### G3 — Only `person_id` filterable (for most viewsets)

**Status: Fixed on dev (#565).**

Clinical list endpoints now support per-model concept, source-concept,
`concept_code`, date-range, and visit filters where the model has a visit FK.
Episodes support concept/source-concept/code/date filters; `visit_occurrence_id`
intentionally remains unsupported for episodes because `Episode` has no visit
FK.

---

### G4 — FHIR ingest covers only 4 resource types

**Status: Fixed on dev (#569, plus earlier v1.1.0 work).**

The FHIR sync endpoint now handles these resource types:

| FHIR Resource         | OMOP Target            |
| --------------------- | ---------------------- |
| Patient               | Person                 |
| Observation           | Measurement            |
| Condition             | ConditionOccurrence    |
| MedicationStatement   | DrugExposure           |
| MedicationRequest     | DrugExposure           |
| AllergyIntolerance    | Observation            |
| Immunization          | DrugExposure           |
| Procedure             | ProcedureOccurrence    |
| DiagnosticReport      | Observation            |
| DocumentReference     | PatientDocument        |

`Procedure` and `DiagnosticReport` — both mentioned in the report as needed —
are covered. `DocumentReference` is now ingested into `PatientDocument` on
`dev`.

---

### G5 — No v1 `/me/` endpoint

**Status: Fixed on dev (#566).**

`/api/patient-info/me/` is the only person-resolution path, and it's on the
frozen legacy prefix. There is no v1 equivalent.

**Current status:** A v1 endpoint exists for an authenticated caller to resolve
their own `person_id`.

---

### G6 — Single-row create has no idempotency

**Status: Fixed on dev (#567).**

The bulk path (JSON array) has full idempotent upsert via `_UPSERT_KEYS`. The
single-row path (JSON object) uses plain `serializer.save()` with no dedup.
The asymmetry is real.

**Former workaround:** As the report notes, sending a one-element list `[{...}]`
instead of a bare object `{...}` activates the bulk upsert path. This is
documented in `CLAUDE.md` and is what existing callers use.

**Current status:** Single-row clinical POST now uses idempotent semantics
consistent with the list/bulk path.

---

### G7 — Service token is one shared secret

**Status: Open (#568, Medium priority). Alternative path exists.**

`ServiceTokenAuthentication` uses a single `SERVICE_AUTH_TOKEN` with a fixed
identity (`urn:service` / `hk-labs-sync`). The report is correct that this
prevents per-service attribution and requires coordinated rotation.

**Context:** PRomop also supports OAuth2 `client_credentials` grants via
`django-oauth-toolkit`, which provides per-client identity, scoped
permissions, and independent rotation. The service token is a convenience for
simple integrations; production service-to-service callers should use OAuth2.

**Current status:** This item remains open. It was triaged as Medium rather
than part of the High-priority fix batch. OAuth2 `client_credentials` remains
the recommended production path for per-client identity and independent
rotation.

---

### G8 — Partial FHIR ingest undetectable

**Status: Fixed on dev (#570).**

The sync response reports per-category counts of what was **written** but does
not report what was **skipped**. A caller sending `n` resources and getting 201
cannot tell whether all `n` arrived.

**Current status:** FHIR sync responses report skipped unsupported resources
and reasons.

---

## Current Fix Set

The High/Critical items from the Vlad batch have been implemented and merged
to `dev` through PRs #582-#590. The only item from this response that remains
open is G7 / #568, which is now tracked separately as Medium priority.
