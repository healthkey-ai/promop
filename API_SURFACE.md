# promop API Surface

> Base URL: `https://promop.onrender.com/api` (production) | `http://localhost:8000/api` (dev)
> Last revised: 2026-06-07

---

## Architecture: OMOP-first, PatientInfo is read-only

**The authoritative clinical record lives in OMOP tables.**

```
Client writes → OMOP tables (Measurement, ConditionOccurrence, DrugExposure, …)
                     │
                     └── post_save / post_delete signal fires automatically
                               │
                               └── refresh_patient_info(person)
                                       re-derives PatientInfo from OMOP
                                       PatientInfo.save()
```

`PatientInfo` is a **denormalized read model**. Callers must not write to it directly. It is regenerated automatically whenever any OMOP record for that patient is saved or deleted.

The two sanctioned write paths are:

| Path | Use case |
|---|---|
| `POST /api/patient-info/upload_fhir/` | Bulk ingest from an EHR / FHIR R4 Bundle |
| `POST/PATCH/DELETE /api/conditions/`, `/api/measurements/`, etc. | Granular OMOP record writes |

The convenience `PATCH /api/patient-info/{person_id}/` endpoint exists for field-level UI updates. It does **not** write to PatientInfo directly — it translates each field into the appropriate OMOP table write (lab/vital fields → `measurement`, others pending OMOP modelling), then the signal chain re-derives PatientInfo.

---

## Table of contents

1. [Authentication & authorization](#authentication--authorization)
2. [Person identity endpoints](#person-identity-endpoints) ← _new (phr-etl integration)_
3. [PatientInfo read endpoints](#patientinfo-read-endpoints)
4. [OMOP write endpoints](#omop-write-endpoints)
   - [PATCH /api/patient-info/{person_id}/ — field update convenience](#patch-apipatient-infoperson_id--field-update-convenience)
   - [POST /api/patient-info/upload_fhir/](#post-apipatient-infoupload_fhir)
   - [DELETE /api/patient-info/bulk_delete/](#delete-apipatient-infobulk_delete)
   - [OMOP clinical event endpoints](#omop-clinical-event-endpoints)
5. [Document & trial endpoints](#document--trial-endpoints)
6. [Vocabulary & concept lookup endpoints](#vocabulary--concept-lookup-endpoints) ← _new (phr-etl integration)_
7. [OAuth2 endpoints](#oauth2-endpoints)
8. [OMOP write internals](#omop-write-internals)
   - [_upsert_omop_measurement](#_upsert_omop_measurement)
   - [_LAB_FIELD_TO_LOINC mapping](#_lab_field_to_loinc-mapping)
   - [FHIR upload pipeline](#fhir-upload-pipeline)
   - [refresh_patient_info signal chain](#refresh_patient_info-signal-chain)
9. [Provenance tagging](#provenance-tagging)
10. [Multi-tenant org scoping](#multi-tenant-org-scoping)

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

## Person identity endpoints

These endpoints implement the phr-etl integration contract (branch `feature/phr-etl-integration`). They allow an external pipeline to resolve a Firebase identity to a stable OMOP `person_id` and to fill in demographic fields without clobbering data that is already present.

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

## PatientInfo read endpoints

`PatientInfo` is a read-only projection of the OMOP tables for a patient. Do not attempt to write clinical data here — write to the OMOP tables instead and PatientInfo will update automatically.

Base path: `/api/patient-info/`
URL parameter `{person_id}` is `Person.person_id` (integer).

---

### GET /api/patient-info/

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

Org-scoped tokens see only patients where `PatientInfo.organization` matches. Superusers see all.

---

### GET /api/patient-info/{person_id}/

Full derived summary for a single patient.

Returns **404** if the caller's org does not own this patient (AUTH-04 row-level scoping).

All field values originate from OMOP tables and are kept current by the signal chain. Do not rely on this endpoint to reflect a write to PatientInfo directly — write to the appropriate OMOP table first.

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
    "...": "all PatientInfo fields"
  },
  "user": {
    "id": 42,
    "username": "patient1001",
    "first_name": "Jane",
    "last_name": "Smith"
  }
}
```

---

### GET /api/patient-info/{person_id}/provenance/

Audit trail: all ProvenanceRecords linked to the patient's PatientInfo row and every OMOP record for that person.

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

## OMOP write endpoints

These are the sanctioned paths for modifying clinical data. All writes ultimately land in OMOP tables; PatientInfo is regenerated automatically by the signal chain.

---

### PATCH /api/patient-info/{person_id}/ — field update convenience

A UI convenience endpoint that accepts PatientInfo field names and **translates them into OMOP table writes**. PatientInfo is **not** written to directly — the signal chain re-derives it after the OMOP write completes.

Returns **403** if patient's org ≠ caller's org.

**Request body** (all fields optional)
```json
{
  "hemoglobin_g_dl": 14.5,
  "wbc_count_thousand_per_ul": 6.8,
  "disease": "Diffuse Large B-Cell Lymphoma",
  "source": "ADMIN_CORRECTION",
  "source_user_id": "dr.jones",
  "modification_reason": "Corrected after lab review"
}
```

`source` choices: `PATIENT_SELF` · `ADMIN_CORRECTION` · `EHR_SYNC` · `DOCUMENT_EXTRACTION`

`modification_reason` is **required** when `source == ADMIN_CORRECTION` — omitting it returns **400**.

**What actually gets written**

For every field in [`_LAB_FIELD_TO_LOINC`](#_lab_field_to_loinc-mapping) present in the request body:

1. `_upsert_omop_measurement(person, field_name, value, today)` writes or updates a row in the `measurement` table.
2. `refresh_patient_info(person)` then re-derives PatientInfo from the updated Measurement rows.
3. If `source` is present, ProvenanceRecords are created for the Measurement row(s).

Fields not yet modelled in OMOP (some behavioral/socioeconomic fields) are patched directly on PatientInfo as a temporary measure until they have a proper OMOP home. This is a transitional state; those fields will move to OMOP tables over time.

**Response 200** — PatientInfo as re-derived from OMOP after the write.

---

### POST /api/patient-info/upload_fhir/

Bulk-ingests one or more patients from a FHIR R4 Bundle. All data is written to OMOP tables; PatientInfo is derived from those records, never written to directly.

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

PatientInfo is **not** a write target. After all OMOP records are saved, `refresh_patient_info(person)` is called explicitly to rebuild PatientInfo from those records. The uploading token's org is stamped on `PatientInfo.organization` at this point.

**Response 200** (HKI-FHIR-02 — OMOP record IDs returned for reconciliation)
```json
{
  "success": true,
  "created_count": 1,
  "updated_count": 0,
  "patients": [
    {
      "person_id": 1001,
      "patient_info_id": 42,
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

### DELETE /api/patient-info/bulk_delete/

Deletes patients and all their OMOP records (via CASCADE). PatientInfo is removed as a cascade consequence.

**Request body**
```json
{ "person_ids": [1001, 1002] }
```

**Response 200**
```json
{ "success": true, "deleted_count": 2, "errors": [] }
```

---

### OMOP clinical event endpoints

These are the direct OMOP write endpoints. They are the canonical way to create, update, or delete individual clinical records. Every write fires a signal that automatically re-derives PatientInfo.

All use `_OmopFilterMixin`:
- `?person_id=X` filters rows to a single patient.
- Org-scoped tokens only see rows whose patient belongs to that org.

| URL | OMOP table written | Filter param |
|---|---|---|
| `/api/conditions/` | `condition_occurrence` | `?person_id=` |
| `/api/drug-exposures/` | `drug_exposure` | `?person_id=` |
| `/api/measurements/` | `measurement` | `?person_id=` |
| `/api/observations/` | `observation` | `?person_id=` |
| `/api/procedures/` | `procedure_occurrence` | `?person_id=` |
| `/api/episodes/` | `episode` (omop_oncology) | `?person_id=` |
| `/api/episode-events/` | `episode_event` | `?episode_id=` |

All support: GET (list + retrieve), POST (create), PUT/PATCH (update), DELETE.

---

## Document & trial endpoints

| URL | Purpose | Filter |
|---|---|---|
| `/api/documents/` | Patient document storage | `?person_id=` |
| `/api/trial-enrollments/` | Clinical trial enrollment status | `?person_id=` |

Full CRUD. Org-scoped. These do not feed into PatientInfo.

---

## Vocabulary & concept lookup endpoints

### GET /api/concepts/lookup/

Batch translate `(vocabulary_id, concept_code)` pairs to OMOP `concept_id`. Used by phr-etl to resolve raw clinical codes before writing OMOP records — unknown codes fall back to `concept_id = 0` on the client side.

Query param `lookup` is repeatable. Each value must be `VOCAB_ID:concept_code`.

**Request**
```
GET /api/concepts/lookup/?lookup=LOINC:2160-0&lookup=LOINC:2345-7&lookup=SNOMED:44054006
```

**Response 200**
```json
{
  "LOINC":  { "2160-0": 3013682, "2345-7": 3000963 },
  "SNOMED": { "44054006": 201826 }
}
```

Unknown codes return `null`. Requires `patient/*.read` scope (read-only).

**Response 400** — no `lookup` params supplied, or a param is missing the `:` separator.

---

### GET /api/vocabularies/{model_name}/

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

## OMOP write internals

### _upsert_omop_measurement

```python
# patient_portal/api/views.py
def _upsert_omop_measurement(person, field_name, value, today):
```

Writes a single lab or vital value into the OMOP `measurement` table. This is the primary write target for numeric clinical observations — PatientInfo is updated downstream by the signal chain.

1. Looks up `(loinc_code, unit, display)` from `_LAB_FIELD_TO_LOINC[field_name]`.
2. Resolves `Concept` by `concept_code = loinc_code, vocabulary_id = 'LOINC'`. Falls back to concept_id 3000963 (generic lab result) if the LOINC Concept is not loaded.
3. **UPDATE** if a row already exists for `(person, concept, date)`.
4. **CREATE** otherwise; `measurement_source_value` = display name (≤ 50 chars); `unit_source_value` = unit string.
5. Saves with `_skip_patient_info_refresh = True` — the caller is responsible for triggering `refresh_patient_info` once, rather than once per measurement row.

Called from `PatientInfoViewSet.partial_update()` for every field in the PATCH body that has a LOINC entry.

---

### _LAB_FIELD_TO_LOINC mapping

Defines which PatientInfo field names map to OMOP `measurement` rows. Any field in this mapping is written to OMOP — not to PatientInfo directly.

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

# Performance status
ecog_performance_status            89247-1    {score}         ECOG Performance Status score
karnofsky_performance_score        89243-0    {score}         Karnofsky Performance Status score
```

---

### FHIR upload pipeline

Every FHIR resource maps to an OMOP table. PatientInfo is never a direct write target.

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
   └── refresh_patient_info(person)   ← explicit call after all OMOP writes complete
         PatientInfo re-derived entirely from the OMOP records written above.
         PatientInfo.organization stamped from the uploading token's org.
         (A small set of fields not yet modelled in OMOP are patched here
          as a transitional measure until they have a proper OMOP table.)
```

---

### refresh_patient_info signal chain

Every write or delete on an OMOP table automatically triggers a PatientInfo rebuild via Django signals. No caller needs to invoke this manually except immediately after a bulk write (e.g. the FHIR upload) where per-row signals are suppressed for performance.

```
OMOP table save / delete
   │
   └── omop_core.signals._refresh_for_instance(instance)
         skipped if instance._skip_patient_info_refresh == True
         │
         └── refresh_patient_info(person)   [omop_core/services/patient_info_service.py]
               1. Clears all _OMOP_DERIVED_FIELDS on PatientInfo
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
                    _get_assessment_data     ← Observation (best_response, RECIST)
                    _get_laboratory_data     ← Measurement (see below)
                    _get_performance_data    ← Observation (ECOG, Karnofsky)
                    _get_genetic_mutations   ← Measurement, LOINC 21636-6/21637-4/21667-1/…
                    _get_cll_data            ← Measurement + Observation + ConditionOccurrence
                    _get_lymphoma_data       ← Observation + Measurement
                    _get_prior_procedures    ← ProcedureOccurrence
               3. _compute_derived_fields   (measurable_disease_imwg, measurable_disease_iwcll, tp53_disruption)
               4. PatientInfo.save()
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

ProvenanceRecords are attached to OMOP rows (Measurement, ConditionOccurrence, DrugExposure, Episode, etc.) — not to PatientInfo itself — since PatientInfo is derived, not authored.

---

## Multi-tenant org scoping

Row-level tenant isolation enforced across all read and write paths (HKI-SEC-04, AUTH-04).

| Endpoint / path | Enforcement |
|---|---|
| `GET /api/patient-info/` | Queryset filtered to `PatientInfo.organization = token.org` |
| `GET /api/patient-info/{person_id}/` | Returns **404** if patient's org ≠ caller's org |
| `PATCH /api/patient-info/{person_id}/` | Returns **403** if patient's org ≠ caller's org |
| All OMOP ViewSets (list) | `_OmopFilterMixin` restricts to persons whose PatientInfo belongs to caller's org |
| `POST upload_fhir/` | Stamps `PatientInfo.organization` from uploading token's org |

Superusers and session-authenticated users bypass org scoping.
