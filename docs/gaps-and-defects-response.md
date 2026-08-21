# PRomop Response to Gaps & Defects Assessment

Response to [promop-gaps-and-defects.md](https://github.com/HealthTree/one/blob/hk/phr-to-promop/docs/promop-gaps-and-defects.md),
verified against `promop@main` at tag `v1.1.0` (2026-08-20).

---

## Status Summary

| #   | Item                                              | Status              | Notes                                                    |
| --- | ------------------------------------------------- | ------------------- | -------------------------------------------------------- |
| D0  | Container can't start with `DEBUG=False`          | **Acknowledged**    | Docker Compose is dev-only; prod deploys via Render/GCP  |
| D1  | Patient can't write own clinical data             | **Acknowledged**    | Fix scoped to viewsets with ownership checks             |
| D2  | Provenance headers fail CORS preflight            | **Acknowledged**    | Two-line settings fix                                    |
| D3  | `find_or_create` mints duplicate Person           | **Acknowledged**    | Agree this is the highest-integrity-risk item            |
| D4  | Second PATCH by same actor 500s                   | **Acknowledged**    | `update_or_create` on the constraint fields              |
| D5  | Partner-auth requests not audited                 | **Acknowledged**    | Truncation + issuer:sub extraction                       |
| D6  | Read/write access disagree at org scope           | **Acknowledged**    | Unify on one predicate with role filter                  |
| D7  | FHIR dedup on display text                        | **Acknowledged**    | Intentional tradeoff; will add concept-based dedup layer |
| D8  | FHIR ingest drops unmapped fields                 | **Acknowledged**    | `referenceRange` and `interpretation` should be mapped   |
| G1  | Clinical reads unpaginated                         | **Acknowledged**    | Will add `LimitOffsetPagination`                         |
| G2  | Unknown filters silently ignored                  | **Acknowledged**    | Agree this is high-priority; strict validation needed    |
| G3  | Only `person_id` filterable                       | **Partially fixed** | `MeasurementViewSet` has extended filters; others do not |
| G4  | FHIR ingest covers only 4 resource types          | **Fixed in v1.1.0** | Now handles 8 resource types including Procedure         |
| G5  | No v1 `/me/` endpoint                             | **Acknowledged**    | Will add `/api/v1/persons/me/`                           |
| G6  | Single-row create has no idempotency              | **Acknowledged**    | Workaround documented; fix planned                       |
| G7  | Service token is one shared secret                | **Acknowledged**    | OAuth2 `client_credentials` exists as alternative path   |
| G8  | Partial FHIR ingest undetectable                  | **Acknowledged**    | Will add `skipped` list to sync response                 |

---

## Detailed Responses

### D0 — Container can't start with `DEBUG=False`

**Status: Acknowledged. Low priority for PRomop's deployment model.**

The report is accurate: `docker-compose.yml` does not pass `ALLOWED_HOSTS` or
`CORS_ALLOWED_ORIGINS`, and the `command:` block has a YAML folded-scalar
formatting issue.

**Context:** PRomop does not deploy via Docker Compose. Production runs on
Render (backend) and GCP Cloud Run (Module Federation remote), both using their
own Dockerfiles (`Dockerfile` and `Dockerfile.gcp`) with env vars injected by
the platform. `docker-compose.yml` exists for local development convenience
with `DEBUG=True`.

**Action:** We will fix both issues — add env var passthrough and fix the
command formatting — so that `docker compose up` works with `DEBUG=False` for
local integration testing. This is a good practice even though it doesn't
affect any deployed environment.

---

### D1 — Patient can't write own clinical data

**Status: Acknowledged. Will fix with scoped permission class.**

`ScopedTokenPermission` on the five clinical viewsets grants unsafe methods
(POST, PUT, DELETE) only to `is_staff` or service-token callers. This blocks
patients and non-staff clinicians from creating clinical rows via the typed
OMOP endpoints.

**Important caveat from the report itself:** `POST /api/v1/fhir/patient-sync/`
uses `IsAuthenticated` and already accepts a patient's own token. The FHIR
sync endpoint is the primary write path for the PHR migration. The typed
endpoints (measurements, observations, etc.) are used for programmatic OMOP
writes, which is a secondary path.

**Action:** Introduce `PatientCrudPermission` on viewsets that carry
`_ProvenanceMixin`, delegating ownership to `can_write_patient`. As the report
notes, this must NOT be applied to `EpisodeEventViewSet`,
`PatientDocumentViewSet`, or `PatientTrialEnrollmentViewSet` — they lack
create-time ownership checks.

---

### D2 — Provenance headers fail CORS preflight

**Status: Acknowledged. Two-line fix.**

`X-Provenance-Source` and `X-Provenance-User-Id` are not in
`CORS_ALLOW_HEADERS`. The fix is adding them to the setting. This is
straightforward.

**Action:** Add both headers to `CORS_ALLOW_HEADERS` in `settings.py`.

---

### D3 — `find_or_create` mints duplicate Person

**Status: Acknowledged. Agree this is the highest-integrity-risk item.**

The report correctly identifies that `find_or_create` looks up by
`actor_iss`/`actor_sub` while the patient portal resolves via `PatientUser`.
When a patient has signed in through the portal (creating a `PatientUser` link
but not populating `actor_iss`/`actor_sub`), a staff-side
`find_or_create` call creates a second `Person`.

**Action:** `find_or_create` will check the `PatientUser` link first, then
backfill `actor_iss`/`actor_sub` on the existing `Person` so both resolution
paths converge. This aligns with the fix described in the report.

---

### D4 — Second PATCH by same actor 500s

**Status: Acknowledged.**

`_record_provenance` uses `ProvenanceRecord.objects.create()`, which violates
the unique constraint `(content_type, object_id, source_user_id, source)` on a
second write by the same actor.

**Action:** Switch to `update_or_create()` keyed on the constraint fields,
updating `changed_at` on match.

---

### D5 — Partner-authenticated requests not audited

**Status: Acknowledged.**

`_get_client_id` falls back to `str(token)`, which for a `TokenClaims` object
can exceed the 255-character `client_id` column, causing a silent `DataError`.
The insert failure is caught and logged as a warning, but the audit row is
lost.

**Action:** Return `f"{token.iss}:{token.sub}"` for `TokenClaims`, truncated to
255 characters. This provides meaningful attribution without risking column
overflow or leaking full token contents into the SIEM stream.

---

### D6 — Read and write access disagree at org scope

**Status: Acknowledged.**

`can_access_patient` checks only `group__memberships__person_id`.
`can_write_patient` additionally checks `org__patients__person_id`. An
org-granted clinician can write data they cannot read back.

**Action:** Unify on a single predicate that honours both the group and org
paths, filtered by role — the role filter is essential because a `patient`-role
`GroupAccess` row must not grant one patient read access to every other patient
in their organization. The existing `PatientRecordIsolationTest` suite (7
tests) already guards against this regression.

---

### D7 — FHIR dedup on display text

**Status: Acknowledged. Intentional tradeoff, but the concern is valid.**

The dedup key in `_upsert_clinical` uses `source_value` (populated from FHIR
`display` text) rather than the resolved `concept_id`. This is documented and
intentional: it allows vocabulary loads to upgrade a stored row's concept
in-place without the row's identity changing.

However, the report's concern is real: two producers spelling the same coded
fact differently (e.g., "Hemoglobin" vs "Hemoglobin [Mass/volume] in Blood")
will create duplicate rows even though PRomop resolves both to the same OMOP
concept. At the cross-producer seam this is a data correctness issue.

**Action:** Add a secondary dedup pass: when `concept_id != 0` and a match
exists on `(concept_id, date)`, treat it as the same event. Fall back to
`source_value` only for `concept_id = 0` rows. Keep `source_value` stored for
provenance — just remove it from the identity key when a concept is resolved.

---

### D8 — FHIR ingest drops unmapped fields

**Status: Acknowledged.**

`referenceRange` maps to OMOP `Measurement.range_low` / `range_high`.
`interpretation` maps to `value_as_concept`. Both have OMOP columns; both are
silently dropped during ingest. The report is correct that this is silent data
loss, especially problematic for a migration that intends to retire the source
copy.

**Action:** Map `referenceRange.low.value` / `referenceRange.high.value` to
`range_low` / `range_high`. Map `interpretation` coding to `value_as_concept`
via concept lookup. Other FHIR fields without OMOP destinations
(`performer`, `bodySite`, `note`) should be reported via G8's `skipped` list
rather than silently dropped.

---

### G1 — Clinical reads are unpaginated

**Status: Acknowledged.**

No `pagination_class` on any of the five clinical viewsets, and no
`DEFAULT_PAGINATION_CLASS` in `REST_FRAMEWORK` settings. A bulk-loaded patient
returns their entire clinical history in one response.

**Action:** Add `LimitOffsetPagination` with a sensible default limit (100) to
the clinical viewsets. This is backward-compatible for callers that don't pass
`limit`/`offset` if we set the default page size high enough, but any caller
expecting a bare list (no envelope) will need updating.

---

### G2 — Unknown query filters silently ignored

**Status: Acknowledged. Agree this is high-priority.**

The report is right that this is the most insidious item: it produces a
**wrong answer** instead of a failure. A caller that asks for
`?observation_date=2026-01-01` gets the whole history and has no way to know
the filter was ignored.

**Action:** Add strict filter validation that returns 400 for unrecognised
query parameters. This is a one-time change at the mixin level that protects
all clinical viewsets.

---

### G3 — Only `person_id` filterable (for most viewsets)

**Status: Partially addressed.**

`MeasurementViewSet` already supports filtering by `measurement_concept_id`,
`measurement_source_concept_id`, `concept_code`, date range
(`measurement_date__gte`/`__lte`), and `visit_occurrence_id`. The other four
clinical viewsets (`ConditionOccurrence`, `DrugExposure`, `Observation`,
`ProcedureOccurrence`) support only `person_id` from `_OmopFilterMixin`.

**Action:** Extend the mixin or each viewset to support concept, date range,
and source_value filtering — these cover the majority of clinical query
patterns. Priority order per the report: concept/code, date range, ordering,
limit.

---

### G4 — FHIR ingest covers only 4 resource types

**Status: Fixed in v1.1.0.**

The FHIR sync endpoint now handles **8 resource types**:

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

`Procedure` and `DiagnosticReport` — both mentioned in the report as needed —
are covered. `DocumentReference` is not yet handled; it will be added when
`PatientDocument` ingestion via FHIR is scoped.

---

### G5 — No v1 `/me/` endpoint

**Status: Acknowledged.**

`/api/patient-info/me/` is the only person-resolution path, and it's on the
frozen legacy prefix. There is no v1 equivalent.

**Action:** Add `/api/v1/persons/me/` that resolves the authenticated caller
to their `Person` record (auto-provisioning if absent), returning `person_id`.
This will be the v1 replacement for the legacy `/api/patient-info/me/` action.

---

### G6 — Single-row create has no idempotency

**Status: Acknowledged. Documented workaround exists.**

The bulk path (JSON array) has full idempotent upsert via `_UPSERT_KEYS`. The
single-row path (JSON object) uses plain `serializer.save()` with no dedup.
The asymmetry is real.

**Workaround:** As the report notes, sending a one-element list `[{...}]`
instead of a bare object `{...}` activates the bulk upsert path. This is
documented in `CLAUDE.md` and is what existing callers use.

**Action:** Either extend the single-row path to use the same natural-key
upsert, or document the list-vs-dict distinction at the endpoint level so
callers don't discover it by accident.

---

### G7 — Service token is one shared secret

**Status: Acknowledged. Alternative path exists.**

`ServiceTokenAuthentication` uses a single `SERVICE_AUTH_TOKEN` with a fixed
identity (`urn:service` / `hk-labs-sync`). The report is correct that this
prevents per-service attribution and requires coordinated rotation.

**Context:** PRomop also supports OAuth2 `client_credentials` grants via
`django-oauth-toolkit`, which provides per-client identity, scoped
permissions, and independent rotation. The service token is a convenience for
simple integrations; production service-to-service callers should use OAuth2.

**Action:** Document OAuth2 `client_credentials` as the recommended path for
production service callers. Consider deprecating the shared-secret path or
limiting it to development/testing.

---

### G8 — Partial FHIR ingest undetectable

**Status: Acknowledged.**

The sync response reports per-category counts of what was **written** but does
not report what was **skipped**. A caller sending `n` resources and getting 201
cannot tell whether all `n` arrived.

**Action:** Add a `skipped` field to the sync response listing each skipped
resource with its type and reason (unsupported type, missing date, parse
error). This also addresses D8 for fields with no OMOP destination — the
caller learns what was dropped rather than assuming completeness.

---

## Suggested Fix Order

We agree with the report's suggested order with minor adjustments:

1. **D0** — unblock local `DEBUG=False` testing
2. **G2** — strict filter validation (highest silent-failure risk)
3. **D1 + D2** — unblock patient and browser-direct writes
4. **D3 + D4 + D5** — data integrity and audit
5. **D6** — org-scope access unification (with role filter)
6. **G5** — v1 person resolution before more v1 consumers arrive
7. **G1 + G3** — pagination and extended filtering
8. **D7 + D8 + G8** — FHIR ingest correctness (dedup, field mapping, skip
   reporting) — all three should land together since they affect the same code
   paths
9. **G6 + G7** — idempotency and service identity (lower urgency; workarounds
   exist)

Fixes for D1-D6 previously existed on `promop@hk/double-write-patient-create`
(closed PR #477). As noted in the report, these will be rebuilt against current
`main` rather than revived from that branch.
