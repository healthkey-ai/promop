# PRomop API Surface

> **Canonical base URL:** `https://promop.onrender.com/api/v1/` (production) | `http://localhost:8000/api/v1/` (dev)
> Last revised: 2026-08-18

> **Versioning note:** All new integrations should target `/api/v1/` paths. The legacy
> unversioned `/api/` paths still work but return `Deprecation: true` / `Sunset: Tue, 01 Dec 2026 00:00:00 GMT`
> headers. The OpenAPI schema and Swagger UI are at `/api/v1/schema/` and `/api/v1/docs/`.

---

## Architecture: OMOP-first, mapped PatientRecord clinical fields are read-only

**The authoritative clinical record lives in OMOP tables.**

```
Client writes → OMOP tables (Measurement, ConditionOccurrence, DrugExposure, …)
                     │
                     └── post_save / post_delete signal fires automatically
                               │
                               └── refresh_patient_record(person)
                                       re-derives PatientRecord from OMOP
                                       PatientRecord.save()
```

`PatientRecord` (Django model: `PatientInfo`, API path: `/api/v1/patient-records/`) is a
**denormalized read model**. Its clinical fields are regenerated automatically whenever
their OMOP source records change, and its profile/admin compatibility fields are copied
from HealthKey extension columns on `Person`. The API rejects writes to
**OMOP-mapped** PatientRecord fields; OMOP APIs, FHIR imports, and `Person`
profile updates own those writes, then the projection refreshes. Unmapped
projection-owned compatibility fields remain temporarily writable only where
the implementation explicitly permits them; new integrations must not use that
exception.

The field-by-field ownership and migration plan is
[`docs/omop_to_patientrecord.md`](docs/omop_to_patientrecord.md). It is the authoritative
answer to which OMOP record supplies each output column; a PatientRecord field name is
never a substitute for a clinical concept, event date, unit, or provenance.

> **Legacy SQL compatibility only:** `public.patient_info` is a read-only database view
> retained solely for existing consumers. New integrations must not query it or depend on
> its column set; use `public.patient_record` for SQL access or `/api/v1/patient-records/`
> for supported application access.

The sanctioned write paths are:

| Path | Use case |
|---|---|
| `POST /api/v1/patient-records/upload_fhir/` | Bulk ingest from an EHR / FHIR R4 Bundle |
| `POST/PATCH/DELETE /api/v1/conditions/`, `/api/v1/measurements/`, etc. | Granular OMOP record writes |
| `PATCH /api/v1/persons/{person_id}/` | Person demographic/profile extension updates |

Mapped PatientRecord fields are read-only. New integrations must use granular OMOP APIs or FHIR
for clinical writes, where concept, time, unit, and provenance are explicit; use
`PATCH /api/v1/persons/{person_id}/` for supported Person profile fields such as email,
phone number, validation metadata, facility name, and demographic redaction preference.

---

## Table of contents

1. [Authentication & authorization](#authentication--authorization)
2. [PatientRecord endpoints](#patientrecord-endpoints) — list, detail, provenance, me, upload_fhir, bulk_delete
3. [OMOP table CRUD](#omop-table-crud) — granular clinical event writes
4. Supplementary API
   - [Person identity endpoints](#person-identity-endpoints)
   - [Document & trial endpoints](#document--trial-endpoints)
   - [Vocabulary & concept lookup endpoints](#vocabulary--concept-lookup-endpoints)
   - [Concept graph endpoints](#concept-graph-endpoints)
   - [Vocabulary release & snapshot (consumer mirror)](#vocabulary-release--snapshot-consumer-mirror)
   - [OAuth2 endpoints](#oauth2-endpoints)
5. [OMOP write internals](#omop-write-internals) — _upsert_omop_measurement, _LAB_FIELD_TO_LOINC, FHIR pipeline, signal chain
6. [Provenance tagging](#provenance-tagging)
7. [Multi-tenant org scoping](#multi-tenant-org-scoping)

---

## Authentication & authorization

All endpoints require authentication.

### Session auth (admin UI / browser)

Standard Django session cookie (`POST /api/auth/login/`). No scope checks applied. Superusers bypass all org scoping.

### OAuth2 Bearer token (service clients / EHR integration)

Tokens must carry SMART on FHIR scopes:

| HTTP methods | Required scope |
|---|---|
| GET, HEAD, OPTIONS | `patient/*.read` or `user/*.read` |
| POST, PUT, PATCH, DELETE | `patient/*.write` or `user/*.write` |

Expired tokens → **401**. Missing or insufficient scopes → **403**.

Grant type: `client_credentials` via `POST /o/token/`

---

## PatientRecord endpoints

`PatientRecord` is the 286-column denormalized projection that is the core of PRomop.
Mapped clinical data enters through OMOP tables or FHIR ingest and is re-derived
automatically. Profile/admin values enter through HealthKey extension columns on
`Person` and are copied into PatientRecord for compatibility.

Base path: `/api/v1/patient-records/`
URL parameter `{person_id}` is `Person.person_id` (integer).

---

### GET /api/v1/patient-records/

List patients visible to the caller's org.

**Response 200**
```json
[
  {
    "id": 1,
    "person_id": 1001,
    "disease": "Breast Cancer",
    "stage": "Stage II",
    "gender": "F",
    "patient_age": 52
  }
]
```

Org-scoped tokens see only patients where `PatientRecord.organization` matches. Superusers see all.

---

### GET /api/v1/patient-records/{person_id}/

Full derived summary for a single patient.

Returns **404** if the caller's org does not own this patient (AUTH-04 row-level scoping).

All field values originate from OMOP tables and are kept current by the signal chain. Do not rely on this endpoint to reflect a write to PatientRecord directly — write to the appropriate OMOP table first.

**Response 200**
```json
{
  "patient_info": {
    "id": 1,
    "person_id": 1001,
    "disease": "Breast Cancer",
    "date_of_birth": "1972-03-15",
    "gender": "F",
    "hemoglobin_g_dl": 11.2,
    "wbc_count_thousand_per_ul": 4.5,
    "serum_creatinine_mg_dl": 0.9,
    "first_line_therapy": "AC-T",
    "first_line_start_date": "2022-03-01",
    "lines_of_therapy": [
      {
        "line": 1,
        "regimen": "AC-T",
        "regimen_concept_id": 35806260,
        "regimen_source": "asserted",
        "release_id": "rel-20260723-a1b2c3",
        "component_ids": [1790099, 1719640],
        "start_date": "2022-03-01",
        "end_date": "2022-09-01",
        "outcome": "CR",
        "intent": "Neoadjuvant",
        "discontinuation_reason": "Completion"
      }
    ],
    "...": "all PatientRecord fields"
  },
  "user": {
    "id": 42,
    "username": "patient1001",
    "first_name": "Jane",
    "last_name": "Smith"
  }
}
```

`lines_of_therapy[]` is a read-only structured view of the flat `first/second/later_*` therapy fields. `line` numbers reflect populated lines only, so the array may not start at 1 and may be non-contiguous (e.g. begins at 2 if 1L is empty, skips a gap if 2L is empty). `regimen_source` is `asserted` or `inferred` (from `therapy_ids_provenance`); while provenance is not yet populated by the derivation pipeline, a resolved `regimen_concept_id` is reported as `inferred` and is never labelled `asserted` — trust `asserted`, verify `inferred`. 3L+ lines are emitted one per later line (from `later_therapies`, including lines whose regimen did not resolve to a concept_id — so `regimen_concept_id` may be `null`), each naming its own regimen with its own dates; their `component_ids`/outcome are the aggregate `later_*` values, flagged `later_aggregate: true` (do not union `component_ids` across `later_aggregate` entries — they repeat the same aggregate set).

---

### GET /api/v1/patient-records/{person_id}/provenance/

Audit trail: all ProvenanceRecords linked to the patient's PatientRecord row and every OMOP record for that person.

**Response 200**
```json
[
  {
    "id": 7,
    "source": "EHR_SYNC",
    "source_user_id": "",
    "modification_reason": null,
    "created_at": "2026-05-10T14:32:00Z",
    "record_type": "measurement",
    "object_id": 23
  }
]
```

---

### GET /api/v1/patient-records/me/

Returns the PatientRecord for the authenticated patient. Only available to patient-scoped tokens (`patient/*.read`); org-admin and clinician tokens receive **404**. Auto-provisioning of a PatientRecord is suppressed for this endpoint to prevent accidental record creation for clinical users who have patient accounts.

**Response 200** — same shape as `GET /api/v1/patient-records/{person_id}/`.

**Response 404** — caller is not a confirmed patient (org_admin, doctor, navigator, or service tokens).

---

### PatientRecord mutation policy

`PATCH /api/v1/patient-records/{person_id}/` returns **405 Method Not Allowed** for every
OMOP-mapped clinical field. It returns the rejected names so callers can migrate without
guessing:

```json
{
  "detail": "OMOP-mapped PatientRecord fields are read-only. Write a complete clinical fact to the appropriate OMOP resource, then rederive the record.",
  "fields": ["hemoglobin_g_dl"]
}
```

Clinical values must be written through their OMOP resources (or FHIR import), after
which the signal chain refreshes `PatientRecord` from OMOP. Profile/admin values that are
displayed on PatientRecord, such as email and validation metadata, are written to HealthKey
extension columns on `Person` via `PATCH /api/v1/persons/{person_id}/` and then projected back.

| PatientRecord output category | Write the source fact to | Required source detail |
|---|---|---|
| Laboratory, vital, tumour-marker, or numeric pathology value | `/api/v1/measurements/` or FHIR `Observation` | clinical concept, known event date, value, unit, provenance |
| Coded clinical, eligibility, disease-state, imaging, or social assertion | `/api/v1/observations/`, `/api/v1/conditions/`, or equivalent FHIR resource | standard concept, known event date, coded/value assertion, provenance |
| Medication or line-of-therapy fact | `/api/v1/drug-exposures/`, `/api/v1/episodes/`, `/api/v1/episode-events/`, or FHIR | medication/episode concept, known dates, provenance |
| Demographic or supported profile value | `PATCH /api/v1/persons/{person_id}/` | the Person source attribute; refresh projects it |

The target state has no writable concrete PatientRecord clinical columns. At
runtime, only fields outside `PATIENT_RECORD_OMOP_MAPPED_FIELDS` may still be
accepted as projection-owned compatibility fields; this temporary exception is
not available to new integrations. The field-level mapping and migration status
are maintained in [`docs/omop_to_patientrecord.md`](docs/omop_to_patientrecord.md).

New integrations should write semantically complete OMOP facts to their own resource
endpoint—for example a dated `Measurement` with its LOINC and unit—or use FHIR ingest.
Include source/provenance on that fact. The PatientRecord API is a read surface, not a
write model for new consumers.

---

### POST /api/v1/patient-records/upload_fhir/

Bulk-ingests one or more patients from a FHIR R4 Bundle. All data is written to OMOP tables; PatientRecord is derived from those records, never written to directly.

**Request** — `multipart/form-data`

| Field | Type | Description |
|---|---|---|
| `file` | File | FHIR R4 Bundle (JSON) |
| `source` | string | Provenance source (also accepted as `X-Provenance-Source` header) |
| `source_user_id` | string | Who triggered the upload (`X-Provenance-User-ID` header also accepted) |
| `modification_reason` | string | Required when `source == ADMIN_CORRECTION` |

**FHIR Bundle structure**

```json
{
  "resourceType": "Bundle",
  "type": "collection",
  "entry": [
    { "resource": { "resourceType": "Patient", "id": "p1", "name": [...], "birthDate": "1970-01-01" } },
    { "resource": { "resourceType": "Condition", "subject": {"reference": "Patient/p1"}, "onsetDateTime": "2022-01-15", "code": {...} } },
    { "resource": { "resourceType": "Observation", "subject": {"reference": "Patient/p1"}, "effectiveDateTime": "2022-02-01",
                    "code": {"coding": [{"system": "http://loinc.org", "code": "718-7"}]}, "valueQuantity": {"value": 11.2} } },
    { "resource": { "resourceType": "MedicationStatement", "subject": {"reference": "Patient/p1"},
                    "medicationCodeableConcept": {"text": "AC-T"}, "effectivePeriod": {"start": "2022-03-01"},
                    "extension": [{"url": "...therapy-line", "valueInteger": 1}, {"url": "...therapy-outcome", "valueString": "CR"}] } }
  ]
}
```

**OMOP tables written per FHIR resource**

| FHIR resource | OMOP table(s) written | Upsert key |
|---|---|---|
| `Patient` | `person`, `users_user` | given_name + family_name + year_of_birth |
| `Condition` | `condition_occurrence` | person + condition_concept + start_date |
| `Observation` | `measurement` | person + measurement_concept + date |
| `MedicationStatement` | `drug_exposure`, `episode`, `episode_event` | person + regimen + start_date |

PatientRecord is **not** a write target. After all OMOP records are saved, `refresh_patient_record(person)` is called explicitly to rebuild PatientRecord from those records. The uploading token's org is stamped on `PatientRecord.organization` at this point.

**Response 200** (HKI-FHIR-02 — OMOP record IDs returned for reconciliation)
```json
{
  "success": true,
  "created_count": 1,
  "updated_count": 0,
  "patients": [
    {
      "person_id": 1001,
      "patient_record_id": 42,
      "measurement_ids": [101, 102, 103],
      "condition_ids": [201],
      "drug_exposure_ids": [301, 302],
      "procedure_ids": [],
      "episode_ids": [401, 402],
      "episode_event_ids": [501, 502]
    }
  ],
  "errors": []
}
```

---

### DELETE /api/v1/patient-records/bulk_delete/

Deletes patients and all their OMOP records (via CASCADE). PatientRecord is removed as a cascade consequence.

**Request body**
```json
{ "person_ids": [1001, 1002] }
```

**Response 200**
```json
{ "success": true, "deleted_count": 2, "errors": [] }
```

---

## Concept graph endpoints

These endpoints expose the OMOP concept graph loaded by `load_athena_vocabularies`, so consumers can traverse:

- regimen → component drugs via `concept_relationship`
- component drug → class / superclass via `concept_ancestor`

Base path: `/api/v1/concepts/...` (v1 only — these endpoints are not registered on the legacy `/api/` URLconf).

All concept graph endpoints require the same OAuth/session auth as the rest of the API. Service clients typically use `patient/*.read`.

Result caps: each source concept returns at most **1000** nodes; when more exist the response includes `"truncated": true` (single-concept endpoints) or lists the capped source ids in `"truncated"` (batch endpoint). The batch endpoint accepts at most **200** `concept_id` parameters.

Direction semantics: without `relationship_id`, traversal uses the `concept_ancestor` closure table (true hierarchy). With `relationship_id`, traversal follows stored edge direction — `ancestors` returns in-neighbors (concepts with an edge pointing *at* the source) and `descendants` returns out-neighbors. For OMOP hierarchical relationships authored child → parent (e.g. `Is a`), use closure mode for true ancestor traversal. Edges with `invalid_reason` set are excluded from relationship-mode traversal.

For background on how PRomop loads and uses `concept`, `concept_relationship`, and `concept_ancestor`, see [docs/concept-mapping.md](docs/concept-mapping.md#concept-graph-api).

### GET /api/v1/concepts/{concept_id}/ancestors/

Returns upstream concepts for one source concept.

Default behavior:

- uses `concept_ancestor`
- excludes the self-row (`ancestor_concept_id == descendant_concept_id`)
- orders by `min_levels_of_separation`, then `concept_id`

Optional query params:

| Param | Meaning |
|---|---|
| `max_levels` | Keep only rows with `min_levels_of_separation <= max_levels` |
| `vocabulary_id` | Repeatable filter on returned concepts |
| `concept_class_id` | Repeatable filter on returned concepts |
| `relationship_id` | If present, switch to direct `concept_relationship` traversal instead of `concept_ancestor` |

Example:

```http
GET /api/v1/concepts/9901002/ancestors/?max_levels=1&vocabulary_id=HemOnc
```

Response:

```json
{
  "concept_id": 9901002,
  "direction": "ancestors",
  "count": 1,
  "truncated": false,
  "results": [
    {
      "concept_id": 9901003,
      "concept_name": "HER2 inhibitor",
      "concept_code": "CLASS-HER2",
      "vocabulary_id": "HemOnc",
      "vocabulary_version": "HemOnc 2024-12-19",
      "concept_class_id": "Drug Class",
      "domain_id": "Drug",
      "standard_concept": null,
      "relationship_id": null,
      "min_levels_of_separation": 1,
      "max_levels_of_separation": 1
    }
  ]
}
```

### GET /api/v1/concepts/{concept_id}/descendants/

Returns downstream concepts for one source concept.

Default behavior:

- uses `concept_ancestor`
- excludes the self-row
- orders by `min_levels_of_separation`, then `concept_id`

If `relationship_id` is supplied, the endpoint switches to direct `concept_relationship` edges. This is the main regimen → component expansion path for HemOnc consumers.

Example:

```http
GET /api/v1/concepts/9901001/descendants/?relationship_id=Has%20targeted%20therapy
```

Response:

```json
{
  "concept_id": 9901001,
  "direction": "descendants",
  "count": 1,
  "truncated": false,
  "results": [
    {
      "concept_id": 9901002,
      "concept_name": "trastuzumab",
      "concept_code": "RX-TRAST",
      "vocabulary_id": "RxNorm",
      "vocabulary_version": "RxNorm 2024-09-03",
      "concept_class_id": "Ingredient",
      "domain_id": "Drug",
      "standard_concept": null,
      "relationship_id": "Has targeted therapy",
      "min_levels_of_separation": null,
      "max_levels_of_separation": null
    }
  ]
}
```

### GET /api/v1/concepts/graph/

Batch traversal endpoint to avoid N+1 calls from consumers.

Required query params:

| Param | Meaning |
|---|---|
| `direction` | `ancestors` or `descendants` |
| `concept_id` | Repeatable source concept id (max 200) |

Optional query params:

| Param | Meaning |
|---|---|
| `relationship_id` | Repeatable direct-edge filter |
| `max_levels` | Ancestor/descendant depth cap when using `concept_ancestor` |
| `vocabulary_id` | Repeatable returned-concept filter |
| `concept_class_id` | Repeatable returned-concept filter |

Example:

```http
GET /api/v1/concepts/graph/?direction=descendants&concept_id=9901001&concept_id=999999&relationship_id=Has%20targeted%20therapy
```

Response:

```json
{
  "direction": "descendants",
  "results": {
    "9901001": [
      {
        "concept_id": 9901002,
        "concept_name": "trastuzumab",
        "concept_code": "RX-TRAST",
        "vocabulary_id": "RxNorm",
        "vocabulary_version": "RxNorm 2024-09-03",
        "concept_class_id": "Ingredient",
        "domain_id": "Drug",
        "standard_concept": null,
        "relationship_id": "Has targeted therapy",
        "min_levels_of_separation": null,
        "max_levels_of_separation": null
      }
    ],
    "999999": []
  },
  "truncated": []
}
```

Unknown `concept_id` keys return empty lists (no per-key 404).

### Error responses

| Case | Status |
|---|---|
| missing `concept_id` on batch endpoint | `400` |
| more than 200 `concept_id` params on batch endpoint | `400` |
| invalid `direction` | `400` |
| non-integer `concept_id` | `400` |
| invalid `max_levels` | `400` |
| unknown concept on single-concept endpoint | `404` |

---

## OMOP table CRUD

Direct read/write access to individual OMOP clinical event tables. Every write fires a signal that automatically re-derives PatientRecord. Use these endpoints when you need granular control over individual clinical records; use `upload_fhir` for bulk ingest.

All use `_OmopFilterMixin`:
- `?person_id=X` filters rows to a single patient.
- Org-scoped tokens only see rows whose patient belongs to that org.

| URL | OMOP table | Filter param |
|---|---|---|
| `/api/conditions/` | `condition_occurrence` | `?person_id=` |
| `/api/drug-exposures/` | `drug_exposure` | `?person_id=` |
| `/api/measurements/` | `measurement` | `?person_id=` |
| `/api/observations/` | `observation` | `?person_id=` |
| `/api/procedures/` | `procedure_occurrence` | `?person_id=` |
| `/api/episodes/` | `episode` (omop_oncology) | `?person_id=` |
| `/api/episode-events/` | `episode_event` | `?episode_id=` |

All support: GET (list + retrieve), POST (create), PUT/PATCH (update), DELETE.

### Detailed FHIR-to-OMOP CRUD sample

See [`docs/examples/fhir_omop_crud.py`](docs/examples/fhir_omop_crud.py) for a
small, runnable example that parses a minimal FHIR bundle shape and exercises
create/retrieve/update/delete for ConditionOccurrence, DrugExposure,
Measurement, Observation, and ProcedureOccurrence. It shows the required event
date, concept, unit, provenance/token setup, and verifies that OMOP writes—not
PatientRecord writes—are the source of the derived projection.

---

## Supplementary API

---

## Person identity endpoints

These endpoints implement the phr-etl integration contract. They allow an external pipeline to resolve an OpenID Connect identity to a stable OMOP `person_id` and fill in demographic fields without clobbering data already present.

Both endpoints require `patient/*.write` scope.

---

### POST /api/persons/find_or_create/

Resolve an OpenID Connect identity (`actor_iss` + `actor_sub`) to a `Person` row, auto-provisioning on first call. The same `(actor_iss, actor_sub)` pair always returns the same `person_id` regardless of which organization or caller invokes it — this is how multi-org identity merge works.

**Request body**
```json
{ "actor_iss": "https://securetoken.google.com/<project>", "actor_sub": "<firebase-uid>" }
```

**Response 201** (new person created)
```json
{ "person_id": 1234, "created": true }
```

**Response 200** (person already exists)
```json
{ "person_id": 1234, "created": false }
```

**Response 400** — `actor_iss` or `actor_sub` missing or blank.

---

### PATCH /api/persons/{person_id}/

Fill-if-empty patch on Person demographic fields. Each field is only written when the existing value is `null` or a recognized placeholder (`""`, `"unknown"`, `"Unknown"`, `1900`, `0`). Real data is never clobbered.

**Request body** (all fields optional)
```json
{
  "given_name": "Jane",
  "family_name": "Doe",
  "year_of_birth": 1980,
  "month_of_birth": 5,
  "day_of_birth": 12,
  "gender_source_value": "female",
  "race_source_value": "White",
  "ethnicity_source_value": "Not Hispanic or Latino"
}
```

**Response 200**
```json
{ "person_id": 1234, "updated_fields": ["given_name", "family_name", "year_of_birth"] }
```

`updated_fields` lists only the fields that were actually written. Fields skipped because the existing value was real are omitted.

**Response 404** — `person_id` not found.

---

## Document & trial endpoints

| URL | Purpose | Filter |
|---|---|---|
| `/api/documents/` | Patient document storage | `?person_id=` |
| `/api/trial-enrollments/` | Clinical trial enrollment status | `?person_id=` |

Full CRUD. Org-scoped. These do not feed into PatientRecord.

---

## Vocabulary & concept lookup endpoints

For a full explanation of how LOINC, SNOMED, and HemOnc codes are resolved to OMOP Concept IDs,
see [docs/concept-mapping.md](docs/concept-mapping.md).

### GET /api/v1/concepts/lookup/

Batch translate `(vocabulary_id, concept_code)` pairs to OMOP `concept_id`. Used by phr-etl to resolve raw clinical codes before writing OMOP records — unknown codes fall back to `concept_id = 0` on the client side.

Query param `lookup` is repeatable. Each value must be `VOCAB_ID:concept_code`.

**Request**
```
GET /api/v1/concepts/lookup/?lookup=LOINC:2160-0&lookup=LOINC:2345-7&lookup=SNOMED:44054006
```

**Response 200**
```json
{
  "LOINC":  { "2160-0": 3013682, "2345-7": 3000963 },
  "SNOMED": { "44054006": 201826 }
}
```

Unknown codes return `null`. Requires `patient/*.read` scope (read-only).

**Opt-in `?include_versions=1`** — adds a top-level `_vocabulary_versions` map (release/version per requested vocabulary) so consumers can pin a release and detect drift. The default `{vocab: {code: id}}` shape is unchanged (phr-etl reads `result[vocab][code]`), so this is additive and off by default.
```json
{
  "LOINC":  { "2160-0": 3013682 },
  "SNOMED": { "44054006": 201826 },
  "_vocabulary_versions": { "LOINC": "LOINC 2.77", "SNOMED": "SNOMED 2024-09-01" }
}
```

**Response 400** — no `lookup` params supplied, or a param is missing the `:` separator.

---

### GET /api/v1/concepts/search/

Search OMOP concepts by case-insensitive substring match on `concept_name`. Use this for
autocomplete, terminology browsing, and finding a candidate `concept_id` when the caller has a
clinical label but not a vocabulary code.

Query params:

| Param | Required | Description |
|---|---:|---|
| `q` | yes | Search string; minimum 3 characters (trigram) after trimming |
| `vocabulary_id` | no | Exact match filter, e.g. `LOINC`, `SNOMED`, `RxNorm`, `HemOnc` |
| `domain_id` | no | Exact match filter, e.g. `Measurement`, `Condition`, `Drug` |
| `concept_class_id` | no | Exact match filter, e.g. `Lab Test`, `Clinical Finding` |
| `standard_concept` | no | Exact match filter; usually `S` for standard or `C` for classification |
| `page` | no | 1-based page number |
| `page_size` | no | Defaults to 25; capped at 100 |

**Request**
```
GET /api/v1/concepts/search/?q=creatinine&vocabulary_id=LOINC&domain_id=Measurement&page_size=10
```

**Response 200**
```json
{
  "count": 2,
  "next": null,
  "previous": null,
  "results": [
    {
      "concept_id": 3016723,
      "concept_name": "Creatinine [Mass/volume] in Serum or Plasma",
      "vocabulary_id": "LOINC",
      "vocabulary_version": "LOINC 2.77",
      "concept_code": "2160-0",
      "domain_id": "Measurement",
      "concept_class_id": "Lab Test",
      "standard_concept": "S"
    }
  ]
}
```

Results are ordered by `concept_id` for stable pagination. Unknown search strings return an
empty paginated result (`count: 0`). Requires `patient/*.read` or `user/*.read` scope.

**Response 400** — `q` is missing or shorter than 3 characters.

---

### GET /api/v1/concepts/{concept_id}/synonyms/

List the synonyms (alternate names) for one concept, so a consumer mirroring promop's vocabulary can cache them.

**Request**
```
GET /api/v1/concepts/7001/synonyms/
```

**Response 200**
```json
{
  "concept_id": 7001,
  "count": 2,
  "results": [
    { "concept_synonym_name": "RVD", "language_concept_id": 4180186 },
    { "concept_synonym_name": "VRd", "language_concept_id": 4180186 }
  ]
}
```

**Response 404** — `concept_id` not found. Requires `patient/*.read` or `user/*.read` scope.

---

### GET /api/v1/concepts/synonyms/

Find concepts by a synonym (alternate name) substring — the reverse of `concepts/lookup/`, for alias resolution (e.g. regimen alias `VRd` → the HemOnc concept). Backed by a GIN trigram index on `concept_synonym_name`.

Query params:

| Param | Required | Description |
|---|---:|---|
| `q` | yes | Synonym substring; minimum 3 characters (trigram) |
| `vocabulary_id` | no | Exact-match filter on the matched concept |
| `concept_class_id` | no | Exact-match filter on the matched concept |
| `page` / `page_size` | no | Pagination; `page_size` capped at 100 |

**Request**
```
GET /api/v1/concepts/synonyms/?q=VRd&vocabulary_id=HemOnc
```

**Response 200**
```json
{
  "count": 1,
  "next": null,
  "previous": null,
  "results": [
    {
      "concept_id": 7001,
      "concept_name": "Bortezomib, Lenalidomide, Dexamethasone",
      "vocabulary_id": "HemOnc",
      "concept_code": "HO-VRD",
      "concept_class_id": "Regimen",
      "standard_concept": "S",
      "concept_synonym_name": "VRd"
    }
  ]
}
```

**Response 400** — `q` is missing or shorter than 3 characters. Requires `patient/*.read` or `user/*.read` scope.

---

### GET /api/v1/concepts/

Browse OMOP concepts with exact-match filters. This is the non-text-search companion to
`/api/v1/concepts/search/`, useful for listing all concepts in a vocabulary/domain/class.

At least one selective filter is required: `vocabulary_id`, `domain_id`, or `concept_class_id`.
`standard_concept` can narrow those results but cannot be the only filter because the full
Athena concept table can contain millions of rows.

Query params:

| Param | Required | Description |
|---|---:|---|
| `vocabulary_id` | conditionally | Exact match filter, e.g. `LOINC`, `SNOMED`, `RxNorm`, `HemOnc` |
| `domain_id` | conditionally | Exact match filter, e.g. `Measurement`, `Condition`, `Drug` |
| `concept_class_id` | conditionally | Exact match filter, e.g. `Lab Test`, `Clinical Finding` |
| `standard_concept` | no | Exact match filter; usually `S` or `C` |
| `page` | no | 1-based page number |
| `page_size` | no | Defaults to 25; capped at 100 |

**Request**
```
GET /api/v1/concepts/?domain_id=Measurement&concept_class_id=Lab%20Test&page_size=25
```

**Response 200**
```json
{
  "count": 1240,
  "next": "http://localhost:8000/api/v1/concepts/?concept_class_id=Lab%20Test&domain_id=Measurement&page=2&page_size=25",
  "previous": null,
  "results": [
    {
      "concept_id": 3016723,
      "concept_name": "Creatinine [Mass/volume] in Serum or Plasma",
      "vocabulary_id": "LOINC",
      "vocabulary_version": "LOINC 2.77",
      "concept_code": "2160-0",
      "domain_id": "Measurement",
      "concept_class_id": "Lab Test",
      "standard_concept": "S"
    }
  ]
}
```

Requires `patient/*.read` or `user/*.read` scope.

**Response 400** — none of `vocabulary_id`, `domain_id`, or `concept_class_id` was supplied.

---

### GET /api/v1/vocabularies/{model_name}/

Returns every entry in a controlled vocabulary table.

**Response 200**
```json
[
  {
    "code": "stage-ii",
    "title": "Stage II",
    "source_name": "AJCC",
    "source_url": "https://www.facs.org/..."
  }
]
```

Available `model_name` slugs (37 total):

`binet-stage` · `cancer-stage` · `disease` · `disease-activity` · `disease-progression` · `distant-metastasis-stage` · `ecog-status` · `estrogen-receptor-status` · `ethnicity` · `flipi-score` · `follicular-lymphoma-grade` · `gelf-criteria` · `her2-status` · `histologic-type` · `hr-status` · `hrd-status` · `infection-status` · `karnofsky-score` · `language` · `language-skill-level` · `measurable-disease` · `morphologic-variant` · `mutation-code` · `mutation-gene` · `mutation-interpretation` · `mutation-origin` · `nodes-stage` · `peripheral-neuropathy-grade` · `pre-existing-condition-category` · `protein-expression` · `richter-transformation` · `staging-modality` · `stem-cell-transplant` · `toxicity-grade` · `tumor-burden` · `tumor-stage`

---

## Vocabulary release & snapshot (consumer mirror)

A consumer (e.g. EXACT) mirrors promop's vocabulary by pinning a **release** and
cross-checking a streamed **snapshot** against the release **manifest**.

| Method | Path | Description |
|---|---|---|
| GET | `/api/v1/vocab-releases/` | List published releases |
| GET | `/api/v1/vocab-releases/latest/` | Current release pointer |
| GET | `/api/v1/vocab-releases/{id}/` | **Manifest** for a release: `id`, `vocab_versions`, `row_counts` (per table), `checksums`, `schema_version`, `scope`, `status`, `published_at` |
| GET | `/api/v1/vocab-releases/{id}/snapshot/{table}/` | **Snapshot**: streaming NDJSON, one row per line, terminated by a `{"__done": true, "rows": N}` sentinel |
| GET | `/api/v1/vocab-releases/latest/snapshot/{table}/` | Snapshot of the current release |

**Snapshot response** is `Content-Type: application/x-ndjson`, `Content-Disposition:
attachment`, carries an `ETag` (supports `If-None-Match` → **304**), and is streamed
from a server-side cursor (constant memory for large tables).

**Completeness gate (consumer side).** A consumer counts the streamed rows and
compares against `row_counts[table]` in the manifest; a missing `__done` sentinel
means the stream was truncated (fail closed). Note the following:

- **Only unfiltered downloads are completeness-checkable.** The `concept` snapshot
  accepts `?source=HealthKey` / `?source=external`, which streams a **subset**;
  `row_counts` is the **full-table** count, so a filtered download will (correctly)
  not match it. Use the unfiltered snapshot for the full-mirror completeness check.
- **The snapshot reads the live table, not an isolated view of the pinned release.**
  If a `load_athena_vocabularies` run mutates a table between a release's publish and
  the consumer's download, the streamed rows can disagree with that release's
  manifest. Loads are infrequent and each publishes a fresh release/ETag, so the
  window is small; treat a mismatch as fail-closed and re-pin to `latest`.
- **Auth is coarse today.** Any token with `patient/*.read` or `user/*.read` can read
  the manifest and snapshot — there is no dedicated system scope for reference data.
  Tightening this is tracked separately (**#344**).

---

## OAuth2 endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/o/token/` | Issue access token (`client_credentials` grant) |
| POST | `/o/revoke_token/` | Revoke a token |
| POST | `/o/introspect/` | Token introspection |
| GET | `/.well-known/smart-configuration` | SMART on FHIR discovery document |
| POST | `/api/auth/login/` | Session login |
| POST | `/api/auth/logout/` | Session logout |

---

## OMOP write and derivation internals

Clinical write APIs operate on OMOP resources, not on projection fields. A numeric
observation must carry its clinical concept, event time, value, and unit; terminology
mapping and canonical-unit policy are documented in
[`docs/concept-mapping.md`](docs/concept-mapping.md) and
[`docs/clinical-unit-policy.md`](docs/clinical-unit-policy.md). This prevents a
lossy projection update from being mistaken for a source clinical fact.

---

### PatientRecord output mapping reference

Shows selected OMOP-to-PatientRecord mappings. This is a derivation/output reference, not
an input API or permission to construct a Measurement from a PatientRecord field name.
New integrations should use the granular OMOP/FHIR representation rather than projection
field names.

```
PatientInfo field                  LOINC      Unit            Display
─────────────────────────────────────────────────────────────────────────────────────────
# CBC
hemoglobin_g_dl                    718-7      g/dL            Hemoglobin [Mass/volume] in Blood
hematocrit_percent                 20570-8    %               Hematocrit [Volume Fraction] of Blood
wbc_count_thousand_per_ul          6690-2     10*3/uL         Leukocytes [#/volume] in Blood
rbc_million_per_ul                 789-8      10*6/uL         Erythrocytes [#/volume] in Blood
platelet_count_thousand_per_ul     777-3      10*3/uL         Platelets [#/volume] in Blood
anc_thousand_per_ul                751-8      10*3/uL         Neutrophils [#/volume] in Blood
alc_thousand_per_ul                731-0      10*3/uL         Lymphocytes [#/volume] in Blood
amc_thousand_per_ul                742-7      10*3/uL         Monocytes [#/volume] in Blood

# CMP / kidney / electrolytes
serum_creatinine_mg_dl             2160-0     mg/dL           Creatinine [Mass/volume] in Serum or Plasma
serum_calcium_mg_dl                17861-6    mg/dL           Calcium [Mass/volume] in Serum or Plasma
egfr_ml_min_173m2                  62238-1    mL/min/1.73m2   GFR/BSA pred CKD-EPI ArA
bun_mg_dl                          3094-0     mg/dL           Urea nitrogen [Mass/volume] in Serum or Plasma
sodium_meq_l                       2951-2     mEq/L           Sodium [Moles/volume] in Serum or Plasma
potassium_meq_l                    2823-3     mEq/L           Potassium [Moles/volume] in Serum or Plasma
magnesium_mg_dl                    2601-3     mg/dL           Magnesium [Mass/volume] in Serum or Plasma
phosphorus                         2777-1     mg/dL           Phosphate [Mass/volume] in Serum or Plasma

# LFT / cardiac
bilirubin_total_mg_dl              1975-2     mg/dL           Bilirubin.total [Mass/volume] in Serum or Plasma
alt_u_l                            1742-6     U/L             Alanine aminotransferase [Enzymatic activity/volume]
ast_u_l                            1920-8     U/L             Aspartate aminotransferase [Enzymatic activity/volume]
alkaline_phosphatase_u_l           6768-6     U/L             Alkaline phosphatase [Enzymatic activity/volume]
albumin_g_dl                       1751-7     g/dL            Albumin [Mass/volume] in Serum or Plasma
total_protein                      2885-2     g/dL            Protein [Mass/volume] in Serum or Plasma
troponin_ng_ml                     10839-9    ng/mL           Troponin I.cardiac [Mass/volume] in Serum or Plasma
bnp_pg_ml                          42637-9    pg/mL           BNP [Mass/volume] in Serum or Plasma
glucose_mg_dl                      2345-7     mg/dL           Glucose [Mass/volume] in Serum or Plasma
hba1c_percent                      4548-4     %               Hemoglobin A1c/Hemoglobin.total in Blood

# Coagulation
inr                                6301-6     {INR}           INR in Platelet poor plasma
pt_seconds                         5902-2     s               Prothrombin time (PT)
ptt_seconds                        3173-2     s               aPTT in Platelet poor plasma

# Oncology markers
ldh_u_l                            2532-0     U/L             Lactate dehydrogenase [Enzymatic activity/volume]
beta2_microglobulin                1952-1     mg/L            Beta-2-Microglobulin [Mass/volume] in Serum or Plasma
c_reactive_protein                 1988-5     mg/L            C reactive protein [Mass/volume] in Serum or Plasma
esr                                30341-2    mm/h            Erythrocyte sedimentation rate
ki67_proliferation_index           85319-2    %               Ki-67 Ag [Presence] in Tissue by Immune stain

# Vital signs
weight                             29463-7    kg              Body weight
height                             8302-2     cm              Body height
systolic_blood_pressure            8480-6     mm[Hg]          Systolic blood pressure
diastolic_blood_pressure           8462-4     mm[Hg]          Diastolic blood pressure
heartrate                          8867-4     /min            Heart rate

# Multiple myeloma disease burden
# Several spellings map to one field: real-world EHR extracts do not agree on a
# single LOINC for the free light chains, and both must project.
monoclonal_protein_serum           51435-6    g/dL            M-protein band 1 [Mass/volume] in Serum by Electrophoresis
monoclonal_protein_serum           33358-3    g/dL            Protein.monoclonal [Mass/volume] in Serum by Electrophoresis
monoclonal_protein_urine           32730-5    mg/24h          Protein.monoclonal [Mass/time] in 24 hour Urine
kappa_flc                          36916-5    mg/dL           Kappa light chains.free [Mass/volume] in Serum
kappa_flc                          80515-0    mg/dL           Kappa light chains.free [Mass/volume] in Serum by nephelometry
lambda_flc                         33944-0    mg/dL           Lambda light chains.free [Mass/volume] in Serum
lambda_flc                         80516-8    mg/dL           Lambda light chains.free [Mass/volume] in Serum by nephelometry
kappa_lambda_ratio                 48378-4    {ratio}         Kappa/Lambda light chains.free [Mass Ratio] in Serum
kappa_lambda_ratio                 80517-6    {ratio}         Kappa/Lambda light chains.free ratio by nephelometry
kappa_lambda_ratio                 104546-7   {ratio}         Kappa/Lambda light chains.free [Mass Ratio] in Serum
clonal_plasma_cells                11118-7    %               Plasma cells/100 cells in Bone marrow

# 33944-8 (kappa) and 33945-5 (lambda) also project, but they are NOT real LOINC
# codes — they exist only as this app's seeded demo concepts and are kept so
# demo patients keep rendering. Note that 33944-8 (seeded kappa) and 33944-0
# (real LOINC lambda) differ by one character and are opposite analytes.
#
# UNITS: kappa_flc and lambda_flc are converted to mg/L from the Measurement's
# unit_source_value (mg/L, mg/dL, mg/100mL, ug/mL, g/L). Labs report FLC in mg/L
# and mg/dL interchangeably — a 10x difference under one field name — so a row
# whose unit is absent or unrecognised is projected UNCONVERTED and logged at
# WARNING. Such a row is not a valid input to any absolute threshold: the SLiM
# light-chain criterion (IMWG 2014: ratio >= 100 AND involved chain >= 100 mg/L)
# treats it as unproven rather than assuming a unit. Senders should always
# populate unit_source_value. Every other field in this table is projected in
# the source's own unit without conversion.

# Performance status
ecog_performance_status            89247-1    {score}         ECOG Performance Status score
karnofsky_performance_score        89243-0    {score}         Karnofsky Performance Status score
```

---

### FHIR upload pipeline

Every FHIR resource maps to an OMOP table. PatientRecord is never a direct write target.

```
FHIR Bundle
   │
   ├── Patient resource
   │     → person               upsert by (given_name, family_name, year_of_birth)
   │     → users_user           create "patient{id}" for new persons only
   │     → ProvenanceRecord     if source provided
   │
   ├── Condition resources  (one per entry with onsetDateTime)
   │     → condition_occurrence  upsert by (person, condition_concept, start_date)
   │     → ProvenanceRecord      if source provided
   │
   ├── Observation resources  (one per entry with effectiveDateTime)
   │     Concept lookup: LOINC code → name match → concept_id 3000963
   │     → measurement            upsert by (person, measurement_concept, date)
   │     → ProvenanceRecord       if source provided
   │
   ├── MedicationStatement resources  (one per therapy line)
   │     → drug_exposure          upsert by (person, regimen, start_date)
   │     → episode                one per therapy-line number (episode_number = LOT)
   │     → episode_event          links drug_exposure → episode
   │     → ProvenanceRecord       if source provided
   │
   └── refresh_patient_record(person)   ← explicit call after all OMOP writes complete
         PatientRecord re-derived entirely from the OMOP records written above.
         PatientRecord.organization stamped from the uploading token's org.
```

---

### refresh_patient_record signal chain

Every write or delete on an OMOP table automatically triggers a PatientRecord rebuild via Django signals. No caller needs to invoke this manually except immediately after a bulk write (e.g. the FHIR upload) where per-row signals are suppressed for performance.

```
OMOP table save / delete
   │
   └── omop_core.signals._refresh_for_instance(instance)
         skipped if instance._skip_patient_record_refresh == True
         │
         └── refresh_patient_record(person)   [omop_core/services/patient_record_service.py]
               1. Clears all _OMOP_DERIVED_FIELDS on PatientRecord
               2. Re-derives every field by querying OMOP tables:
                    _get_demographics        ← Person (age, gender, ethnicity, languages)
                    _get_location_data       ← Location (country, region, city, postal_code)
                    _get_disease_data        ← ConditionOccurrence (disease, diagnosis_date, slug)
                    _get_treatment_data      ← DrugExposure / Episode (therapy lines)
                    _get_vitals_data         ← Measurement, LOINC 8480-6/8462-4/8867-4/29463-7/8302-2
                    _get_biomarker_data      ← Measurement, LOINC 85337-4/16112-5/16113-3/48676-1
                    _get_social_data         ← Observation (employment, insurance)
                    _get_behavior_data       ← Observation (tobacco use)
                    _get_infection_data      ← Measurement, LOINC 5221-7/5195-3/5196-1
                    _get_assessment_data     ← Observation (RECIST)
                    _get_laboratory_data     ← Measurement (see below)
                    _get_performance_data    ← Observation (ECOG, Karnofsky)
                    _get_genetic_mutations   ← Measurement, LOINC 21636-6/21637-4/21667-1/…
                    _get_cll_data            ← Measurement + Observation + ConditionOccurrence
                    _get_lymphoma_data       ← Observation + Measurement
                    _get_prior_procedures    ← ProcedureOccurrence
               3. _compute_derived_fields   (measurable_disease_imwg, measurable_disease_iwcll, tp53_disruption)
               4. PatientRecord.save()
```

**_get_laboratory_data lookup strategy:**

```
1. LOINC concept code (primary)
      Measurement JOIN Concept
      WHERE concept_code IN _LOINC_LAB_FIELDS
        AND vocabulary_id = 'LOINC'
      → populates hemoglobin_g_dl, wbc_count_thousand_per_ul, serum_creatinine_mg_dl, etc.

2. measurement_source_value fallback (when LOINC Concepts are not loaded in Concept table)
      Measurement WHERE measurement_source_value IN _SOURCE_VALUE_LAB_FIELDS
      → same field set, matched by the display string stored at write time
```

Most-recent measurement wins for each field (ORDER BY measurement_date DESC).

---

## Provenance tagging

Every OMOP write can carry a provenance source. `ProvenanceRecord` stores a generic FK to the written OMOP instance.

| Source value | Meaning |
|---|---|
| `PATIENT_SELF` | Patient entered data themselves |
| `ADMIN_CORRECTION` | Admin correction on behalf of patient (`modification_reason` required) |
| `EHR_SYNC` | Automated EHR system push |
| `DOCUMENT_EXTRACTION` | AI-extracted from a clinical document |

ProvenanceRecords are attached to OMOP rows (Measurement, ConditionOccurrence, DrugExposure, Episode, etc.) — not to PatientRecord itself — since PatientRecord is derived, not authored.

---

## Multi-tenant org scoping

Row-level tenant isolation enforced across all read and write paths (HKI-SEC-04, AUTH-04).

| Endpoint / path | Enforcement |
|---|---|
| `GET /api/v1/patient-records/` | Queryset filtered to `PatientRecord.organization = token.org` |
| `GET /api/v1/patient-records/{person_id}/` | Returns **404** if patient's org ≠ caller's org |
| Mapped clinical fields on `/api/v1/patient-records/{person_id}/` | Read-only; clinical writes belong to scoped OMOP endpoints/imports |
| Profile/admin fields displayed on PatientRecord | Read-only projection from scoped `Person` extension writes |
| All OMOP ViewSets (list) | `_OmopFilterMixin` restricts to persons whose PatientRecord belongs to caller's org |
| `POST /api/v1/patient-records/upload_fhir/` | Stamps `PatientRecord.organization` from uploading token's org |

Superusers and session-authenticated users bypass org scoping.
