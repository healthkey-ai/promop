# ctomop API Surface

> Base URL: `https://ctomop.onrender.com/api` (production) | `http://localhost:8000/api` (dev)
> Last revised: 2026-05-11

---

## Table of contents

1. [Authentication & authorization](#authentication--authorization)
2. [PatientInfo endpoints](#patientinfo-endpoints)
3. [OMOP clinical event endpoints](#omop-clinical-event-endpoints)
4. [Document & trial endpoints](#document--trial-endpoints)
5. [Vocabulary endpoint](#vocabulary-endpoint)
6. [OAuth2 endpoints](#oauth2-endpoints)
7. [OMOP write paths](#omop-write-paths)
   - [PATCH write-through: _upsert_omop_measurement](#patch-write-through-_upsert_omop_measurement)
   - [_LAB_FIELD_TO_LOINC mapping](#_lab_field_to_loinc-mapping)
   - [FHIR upload pipeline](#fhir-upload-pipeline)
   - [refresh_patient_info signal chain](#refresh_patient_info-signal-chain)
8. [Provenance tagging](#provenance-tagging)
9. [Multi-tenant org scoping](#multi-tenant-org-scoping)

---

## Authentication & authorization

All endpoints require authentication. Two paths are supported.

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

## PatientInfo endpoints

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

Full detail for a single patient.

Returns **404** if the caller's org does not own this patient (AUTH-04 row-level scoping).

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

### PATCH /api/patient-info/{person_id}/

Partially updates a patient. **Lab and vital fields are written through to the OMOP Measurement table** in addition to PatientInfo (HKI-PDS-01).

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

**OMOP write side-effects**

For every field in [`_LAB_FIELD_TO_LOINC`](#_lab_field_to_loinc-mapping) present in the request body:

1. `_upsert_omop_measurement(person, field_name, value, today)` — creates or updates a row in the `measurement` table.
2. If `source` is present, a `ProvenanceRecord` is created for both the PatientInfo update and the Measurement row.

Returns **403** if patient's org ≠ caller's org.

**Response 200** — full updated PatientInfo.

---

### GET /api/patient-info/{person_id}/provenance/

Audit trail: all ProvenanceRecords linked to this patient's PatientInfo row and every OMOP record for that person.

**Response 200**
```json
[
  {
    "id": 7,
    "source": "EHR_SYNC",
    "source_user_id": "",
    "modification_reason": null,
    "created_at": "2026-05-10T14:32:00Z",
    "record_type": "patientinfo",
    "object_id": 1
  },
  {
    "id": 8,
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

### POST /api/patient-info/upload_fhir/

Bulk-ingests one or more patients from a FHIR R4 Bundle. Primary write path for EHR integration.

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

| FHIR resource | OMOP table(s) | Upsert key |
|---|---|---|
| `Patient` | `person`, `users_user` | given_name + family_name + year_of_birth |
| `Condition` | `condition_occurrence` | person + condition_concept + start_date |
| `Observation` | `measurement` | person + measurement_concept + date |
| `MedicationStatement` | `drug_exposure`, `episode`, `episode_event` | person + regimen + start_date |

Observation → Concept lookup order:
1. LOINC code → `Concept WHERE concept_code = loinc_code AND vocabulary_id = 'LOINC'`
2. Observation text → `Concept WHERE concept_name ILIKE obs_name[:50]`
3. Fallback → `Concept WHERE concept_id = 3000963` (generic lab result)

After all OMOP records are written, `refresh_patient_info(person)` re-derives the PatientInfo from OMOP tables. Fields not yet modelled in OMOP (behavioral, socioeconomic, some staging flags) are patched directly. The uploading token's org is stamped on `PatientInfo.organization`.

**Response 200** (HKI-FHIR-02 — record IDs returned for reconciliation)
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

Deletes patients and all their OMOP records (via CASCADE).

**Request body**
```json
{ "person_ids": [1001, 1002] }
```

**Response 200**
```json
{ "success": true, "deleted_count": 2, "errors": [] }
```

---

## OMOP clinical event endpoints

All use `_OmopFilterMixin`:
- `?person_id=X` filters rows to a single patient.
- Org-scoped tokens only see rows whose patient belongs to that org.

Writing to any of these tables fires a `post_save`/`post_delete` signal that calls `refresh_patient_info(person)`.

| URL | OMOP table | Filter param |
|---|---|---|
| `/api/conditions/` | `condition_occurrence` | `?person_id=` |
| `/api/drug-exposures/` | `drug_exposure` | `?person_id=` |
| `/api/measurements/` | `measurement` | `?person_id=` |
| `/api/observations/` | `observation` | `?person_id=` |
| `/api/procedures/` | `procedure_occurrence` | `?person_id=` |
| `/api/episodes/` | `episode` (omop_oncology) | `?person_id=` |
| `/api/episode-events/` | `episode_event` | `?episode_id=` |

All support the standard DRF router verbs: GET (list + retrieve), POST, PUT/PATCH, DELETE.

---

## Document & trial endpoints

| URL | Purpose | Filter |
|---|---|---|
| `/api/documents/` | Patient document storage | `?person_id=` |
| `/api/trial-enrollments/` | Clinical trial enrollment status | `?person_id=` |

Full CRUD. Org-scoped.

---

## Vocabulary endpoint

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

## OMOP write paths

### PATCH write-through: _upsert_omop_measurement

```python
# patient_portal/api/views.py
def _upsert_omop_measurement(person, field_name, value, today):
```

Creates or updates a single row in the OMOP `measurement` table for a lab or vital field.

1. Looks up `(loinc_code, unit, display)` from `_LAB_FIELD_TO_LOINC[field_name]`.
2. Resolves `Concept` by `concept_code = loinc_code, vocabulary_id = 'LOINC'`. Falls back to concept_id 3000963 (generic lab result) if not found.
3. **UPDATE** if a row already exists for `(person, concept, date)`.
4. **CREATE** otherwise; `measurement_source_value` = display name (≤ 50 chars); `unit_source_value` = unit string.
5. Saves with `_skip_patient_info_refresh = True` to suppress the signal during bulk writes.

Called from `PatientInfoViewSet.partial_update()` for every field in the PATCH body that has a LOINC entry.

---

### _LAB_FIELD_TO_LOINC mapping

Every PatientInfo field that triggers an OMOP Measurement write when PATCHed.

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

# LFT / cardiac (added FHIR-08)
bilirubin_total_mg_dl              1975-2     mg/dL           Bilirubin.total [Mass/volume] in Serum or Plasma
alt_u_l                            1742-6     U/L             Alanine aminotransferase [Enzymatic activity/volume]
ast_u_l                            1920-8     U/L             Aspartate aminotransferase [Enzymatic activity/volume]
alkaline_phosphatase_u_l           6768-6     U/L             Alkaline phosphatase [Enzymatic activity/volume]
albumin_g_dl                       1751-7     g/dL            Albumin [Mass/volume] in Serum or Plasma
total_protein                      2885-2     g/dL            Protein [Mass/volume] in Serum or Plasma        ← added
troponin_ng_ml                     10839-9    ng/mL           Troponin I.cardiac [Mass/volume] in Serum or Plasma  ← added
bnp_pg_ml                          42637-9    pg/mL           BNP [Mass/volume] in Serum or Plasma                ← added
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

Fields marked `← added` were introduced in FHIR-08 to complete LFT/cardiac coverage.

---

### FHIR upload pipeline

Sequence of OMOP writes for `POST /api/patient-info/upload_fhir/`:

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
   │     Concept lookup: LOINC code → name match → concept_id 3000963  (FHIR-06/07/08)
   │     → measurement            upsert by (person, measurement_concept, date)
   │     → ProvenanceRecord       if source provided
   │
   ├── MedicationStatement resources  (one per therapy line)
   │     → drug_exposure          upsert by (person, regimen, start_date)
   │     → episode                one per therapy-line number (episode_number = LOT)
   │     → episode_event          links drug_exposure → episode
   │     → ProvenanceRecord       if source provided
   │
   └── refresh_patient_info(person)
         Clears and re-derives _OMOP_DERIVED_FIELDS from OMOP tables.
         Direct-patch of fields not yet in OMOP (behavioral, socioeconomic, etc.).
         Stamps PatientInfo.organization from the uploading token's org.
```

**Change in FHIR-06/07/08/09:** Observation → Measurement mapping now attempts LOINC-based `Concept` lookup before falling back to name matching. Lab fields (CBC, CMP, LFT/cardiac) are no longer written directly to PatientInfo; instead `refresh_patient_info` derives them from Measurement via `_get_laboratory_data`.

---

### refresh_patient_info signal chain

Any save or delete on an OMOP table fires a Django signal that calls `refresh_patient_info`:

```
OMOP table write (save / delete)
   │
   └── omop_core.signals._refresh_for_instance(instance)
         skipped if instance._skip_patient_info_refresh == True
         │
         └── refresh_patient_info(person)   [omop_core/services/patient_info_service.py]
               1. Clears all _OMOP_DERIVED_FIELDS on PatientInfo
               2. Calls section extractors (each returns {field: value} dict):
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
                    _get_genetic_mutations   ← Measurement, LOINC 21636-6/21637-4/21667-1/48013-7/62862-8/62318-1
                    _get_cll_data            ← Measurement + Observation + ConditionOccurrence
                    _get_lymphoma_data       ← Observation + Measurement
                    _get_prior_procedures    ← ProcedureOccurrence
               3. _compute_derived_fields   (measurable_disease_imwg, measurable_disease_iwcll, tp53_disruption)
               4. PatientInfo.save()
```

**_get_laboratory_data lookup strategy** (FHIR-09):

```
1. LOINC concept code (primary)
      Measurement JOIN Concept
      WHERE concept_code IN _LOINC_LAB_FIELDS
        AND vocabulary_id = 'LOINC'
      → maps to hemoglobin_g_dl, wbc_count_thousand_per_ul, serum_creatinine_mg_dl, etc.

2. measurement_source_value fallback (for environments without LOINC Concepts loaded)
      Measurement
      WHERE measurement_source_value IN _SOURCE_VALUE_LAB_FIELDS
      → same field set, matched by the display string stored during write
```

Both strategies take the most-recent measurement per field (ORDER BY measurement_date DESC).

---

## Provenance tagging

Every clinical write can carry a provenance source. `ProvenanceRecord` stores a generic FK to any model instance.

| Source value | Meaning |
|---|---|
| `PATIENT_SELF` | Patient entered data themselves |
| `ADMIN_CORRECTION` | Admin correction on behalf of patient (`modification_reason` required) |
| `EHR_SYNC` | Automated EHR system push |
| `DOCUMENT_EXTRACTION` | AI-extracted from a clinical document |

ProvenanceRecords are created for every OMOP row during FHIR upload, and for PatientInfo + Measurement during PATCH, when `source` is present in the request.

---

## Multi-tenant org scoping

Row-level tenant isolation is enforced across all read and write paths (HKI-SEC-04, AUTH-04).

| Endpoint / path | Enforcement |
|---|---|
| `GET /api/patient-info/` | Queryset filtered to `PatientInfo.organization = token.org` |
| `GET /api/patient-info/{person_id}/` | Returns **404** if patient's org ≠ caller's org |
| `PATCH /api/patient-info/{person_id}/` | Returns **403** if patient's org ≠ caller's org |
| All OMOP ViewSets (list) | `_OmopFilterMixin` restricts to persons whose PatientInfo belongs to caller's org |
| `POST upload_fhir/` | Stamps `PatientInfo.organization` from uploading token's org |

Superusers and session-authenticated users bypass org scoping.
