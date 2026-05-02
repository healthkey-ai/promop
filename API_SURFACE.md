# CTOMOP API Surface

> Auto-generated from source analysis — 2026-04-30
>
> Base URL: `https://ctomop.onrender.com/api` (production) | `http://localhost:8000/api` (dev)

## Overview

ctomop exposes a REST API built with Django REST Framework that serves as the backend for a clinical oncology patient portal. The API provides:

- **Session-based authentication** — login/logout endpoints that issue Django session cookies.
- **Patient CRUD** — list, create, read, update, and bulk-delete patient records stored in an OMOP CDM-aligned PostgreSQL schema. Each patient record (`PatientInfo`) carries 200+ clinical fields spanning demographics, labs, treatment history, oncology biomarkers, behavioral data, and disease-specific assessments.
- **FHIR R4 ingestion** — accepts a FHIR Bundle JSON file and automatically maps Patient, Observation (60+ LOINC codes), Condition, and MedicationStatement resources into the internal data model.
- **CSV ingestion** — bulk-creates patients from a simple CSV upload.
- **Health check** — lightweight probe for monitoring and deploy verification.

All data-mutating endpoints (except FHIR upload) require an authenticated session. The frontend is a React/TypeScript SPA that communicates with these endpoints via Axios, forwarding the session cookie and CSRF token on every request.

---

## Table of Contents

1. [Authentication](#1-authentication)
2. [Health Check](#2-health-check)
3. [Current User](#3-current-user)
4. [Patient Info — CRUD](#4-patient-info--crud)
5. [Patient Info — Bulk Delete](#5-patient-info--bulk-delete)
6. [CSV Upload](#6-csv-upload)
7. [FHIR Bundle Upload](#7-fhir-bundle-upload)
8. [Authentication & Security Model](#8-authentication--security-model)
9. [PatientInfo Schema (Full Field Reference)](#9-patientinfo-schema-full-field-reference)
10. [Person (OMOP CDM) Schema](#10-person-omop-cdm-schema)
11. [Serializer Summary](#11-serializer-summary)
12. [FHIR LOINC Code Mapping](#12-fhir-loinc-code-mapping)
13. [Frontend Integration Notes](#13-frontend-integration-notes)

---

## 1. Authentication

Session-based auth using Django's built-in session framework. The login endpoint returns a `sessionid` cookie that must be included in subsequent requests. No token-based auth (JWT/OAuth) is supported.

### `POST /api/auth/login/`

Authenticate with username and password. On success, sets a `sessionid` cookie and returns the user profile.

| Property | Value |
|----------|-------|
| Auth required | No |
| CSRF exempt | Yes |
| Content-Type | `application/json` |

**Request:**
```json
{
  "username": "string",
  "password": "string"
}
```

**Response `200`:**
```json
{
  "message": "Login successful",
  "user": {
    "id": 1,
    "username": "admin",
    "email": "admin@example.com",
    "first_name": "Admin",
    "last_name": "User"
  }
}
```

**Errors:** `400` missing fields, `401` invalid credentials.

---

### `POST /api/auth/logout/`

Invalidates the current session cookie and logs the user out.

| Property | Value |
|----------|-------|
| Auth required | No |
| CSRF exempt | Yes |

**Response `200`:**
```json
{ "message": "Logged out successfully" }
```

---

### `POST /api/auth/test/`

Diagnostic endpoint for debugging authentication issues. Returns details about the current session state, headers, and cookie presence. Not intended for production use.

| Property | Value |
|----------|-------|
| Auth required | No |
| CSRF exempt | Yes |

---

## 2. Health Check

Lightweight probe used by Render's health check and uptime monitoring. Verifies the service is running and the database connection is alive.

### `GET /api/health/`

| Property | Value |
|----------|-------|
| Auth required | No |

**Response `200`:**
```json
{
  "status": "healthy",
  "service": "ctomop",
  "database": "connected"
}
```

---

## 3. Current User

Returns the profile of the currently authenticated user. Used by the frontend on page load to verify the session is still valid and to display the logged-in user's name.

### `GET /api/user/`

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |

**Response `200`:**
```json
{
  "user": {
    "id": 1,
    "username": "admin",
    "email": "admin@example.com",
    "first_name": "Admin",
    "last_name": "User"
  }
}
```

---

## 4. Patient Info — CRUD

Core CRUD operations on `PatientInfo` records. Each patient is keyed by `person_id` (from the OMOP CDM `Person` table). The list endpoint returns a lightweight summary; the detail endpoint returns the full 200+ field record. Lookups use `person_id` as the URL path parameter, not the Django auto-PK.

### `GET /api/patient-info/`

Returns all patients as a summary list, ordered by most recently created first. Uses `PatientListSerializer` with only six fields for fast rendering of the patient table in the frontend.

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |

**Response `200`:**
```json
[
  {
    "id": 1,
    "person_id": 1001,
    "patient_name": "Jane Doe",
    "age": 54,
    "disease": "Breast Cancer",
    "stage": "IIIA",
    "updated_at": "2026-04-30"
  }
]
```

Uses `PatientListSerializer` — lightweight fields only.

---

### `POST /api/patient-info/`

Create a new patient record. Accepts any subset of `PatientInfo` fields. A corresponding `Person` record must already exist or be created as part of the request.

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |
| Content-Type | `application/json` |

**Request body:** Any subset of the [PatientInfo schema](#9-patientinfo-schema-full-field-reference).

**Response `201`:** Full `PatientInfoSerializer` output.

---

### `GET /api/patient-info/{person_id}/`

Retrieve the full clinical record for a single patient, including all labs, treatment history, biomarkers, and behavioral data. Also returns the associated user profile.

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |
| Path param | `person_id` — integer (`Person.person_id`) |

**Response `200`:**
```json
{
  "patient_info": { /* full PatientInfo schema */ },
  "user": {
    "id": 1,
    "username": "admin",
    "email": "admin@example.com",
    "first_name": "Admin",
    "last_name": "User"
  }
}
```

**Errors:** `404` if Person or PatientInfo not found.

---

### `PUT /api/patient-info/{person_id}/`

Replace the entire patient record. All fields must be provided; omitted fields will be set to null/default.

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |
| Path param | `person_id` — integer |
| Content-Type | `application/json` |

**Request body:** Complete PatientInfo schema.  
**Response `200`:** Updated PatientInfo.

---

### `PATCH /api/patient-info/{person_id}/`

Update specific fields on a patient record without affecting others. This is the primary endpoint used by the frontend's inline-edit forms.

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |
| Path param | `person_id` — integer |
| Content-Type | `application/json` |

**Request body:** Any subset of PatientInfo fields.  
**Response `200`:** Updated PatientInfo.

---

### `PATCH /api/patient-info/{person_id}/update_patient/`

Dedicated partial-update action routed as a DRF custom action. Functionally equivalent to `PATCH` on the detail endpoint, but provides an explicit named action for clarity in the URL.

| Property | Value |
|----------|-------|
| Auth required | Yes |
| Path param | `person_id` — integer |

**Response `200`:** Updated PatientInfo.  
**Errors:** `404` if Person or PatientInfo not found.

---

## 5. Patient Info — Bulk Delete

Allows deleting multiple patients in a single request. Cascades to the associated `Person` and Django `User` records. Used by the frontend's multi-select delete action in the patient list.

### `DELETE /api/patient-info/bulk_delete/`

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |
| Content-Type | `application/json` |

**Request:**
```json
{
  "person_ids": [1001, 1002, 1003]
}
```

**Response `200`:**
```json
{
  "success": true,
  "deleted_count": 3,
  "errors": []
}
```

---

## 6. CSV Upload

Simple bulk import for basic patient demographics. Accepts a CSV file with minimal columns (`person_id`, `year_of_birth`, `gender`, `date_of_birth`, `disease`). Creates `Person` + `PatientInfo` records for each row. Rows that fail validation are reported in the `errors` array without blocking other rows.

### `POST /api/patient-info/upload_csv/`

| Property | Value |
|----------|-------|
| Auth required | Yes |
| CSRF exempt | Yes |
| Content-Type | `multipart/form-data` |

**Request:** Form field `file` containing a CSV with headers:

| CSV Column | Required | Notes |
|------------|----------|-------|
| `person_id` | No | Auto-generates if 0 or absent |
| `year_of_birth` | Yes | Integer |
| `gender` | Yes | `male`/`m`/`female`/`f`/`unknown`/`other`/`ambiguous` |
| `date_of_birth` | No | `YYYY-MM-DD` or `MM/DD/YYYY` |
| `disease` | Yes | Free text |

**Response `200`:**
```json
{
  "success": true,
  "created_count": 50,
  "errors": ["Row 12: invalid year_of_birth"]
}
```

---

## 7. FHIR Bundle Upload

The most complex endpoint in the system. Accepts a FHIR R4 Bundle (JSON file upload) and maps its resources into the internal OMOP-aligned data model. It parses `Patient` resources for demographics, `Observation` resources for labs/biomarkers/vitals (60+ LOINC codes), `Condition` resources for diagnoses and staging, and `MedicationStatement` resources for treatment history. Each patient in the bundle produces a `Person`, `PatientInfo`, plus optional `ConditionOccurrence` and `Measurement` records. This endpoint is publicly accessible (no auth required) to support automated ingestion pipelines.

### `POST /api/patient-info/upload_fhir/`

| Property | Value |
|----------|-------|
| Auth required | No (AllowAny) |
| CSRF exempt | Yes |
| Content-Type | `multipart/form-data` |

**Request:** Form field `file` containing a FHIR R4 Bundle JSON.

**Response `200`:**
```json
{
  "success": true,
  "created_count": 10,
  "errors": []
}
```

### Supported FHIR Resource Types

| FHIR Resource | Maps To |
|---------------|---------|
| `Patient` | `Person` + demographics in `PatientInfo` |
| `Condition` | `PatientInfo` staging fields + `ConditionOccurrence` |
| `Observation` | Labs, vitals, biomarkers, behavioral fields in `PatientInfo` + `Measurement` |
| `MedicationStatement` | Therapy line fields in `PatientInfo` |

### Patient Resource Extraction

| FHIR Path | Target Field |
|-----------|-------------|
| `id` | Internal patient reference |
| `birthDate` | `date_of_birth` |
| `name[0].given[]` | `Person.given_name` |
| `name[0].family` | `Person.family_name` |
| `gender` | `Person.gender_concept` (OMOP mapped) |
| `address[0].country` | `country` |
| `address[0].state` | `region` |
| `address[0].city` | `city` |
| `address[0].postalCode` | `postal_code` |
| Extension `ethnicity` | `ethnicity` |
| Extension `bodyWeight` | `weight` |
| Extension `bodyHeight` | `height` |
| Extension `systolic-bp` | `systolic_blood_pressure` |
| Extension `diastolic-bp` | `diastolic_blood_pressure` |
| Extension `heartRate` | `heart_rate` |
| Extension `ecog-performance-status` | `ecog_performance_status` |

### MedicationStatement Extraction

| FHIR Path | Target |
|-----------|--------|
| Extension `therapy-line` | Routes to `first_line_*` / `second_line_*` / `later_*` fields |
| Extension `therapy-outcome` | `*_outcome` |
| `medicationCodeableConcept.text` | `*_therapy` (regimen name) |
| `effectivePeriod.start` | `*_start_date` |
| `effectivePeriod.end` | `*_end_date` |

See [FHIR LOINC Code Mapping](#12-fhir-loinc-code-mapping) for all supported Observation codes.

---

## 8. Authentication & Security Model

Describes how the API authenticates requests, handles CSRF, and configures CORS. All API views bypass CSRF via `@csrf_exempt` (the frontend sends the token anyway as a defense-in-depth measure). CORS is fully open — intended for development; should be locked down for production.

### Authentication Classes

- **SessionAuthentication** — Django session cookies (primary)
- **BasicAuthentication** — HTTP Basic Auth (fallback)

### Default Permission

`AllowAny` in REST_FRAMEWORK settings, overridden per-view.

### CORS

- All origins allowed (`CORS_ALLOW_ALL_ORIGINS = True`)
- Credentials allowed (`CORS_ALLOW_CREDENTIALS = True`)

### CSRF

Enabled globally via `CsrfViewMiddleware`. Bypassed with `@csrf_exempt` on all API endpoints. Frontend sends `X-CSRFToken` header extracted from the `csrftoken` cookie.

### Middleware Stack (order)

1. `SecurityMiddleware`
2. `WhiteNoiseMiddleware` (static files)
3. `CorsMiddleware`
4. `SessionMiddleware`
5. `CommonMiddleware`
6. `CsrfViewMiddleware`
7. `AuthenticationMiddleware`
8. `MessageMiddleware`
9. `XFrameOptionsMiddleware`

---

## 9. PatientInfo Schema (Full Field Reference)

The central data model of the application. `PatientInfo` is a one-to-one extension of the OMOP CDM `Person` table that stores all clinical, laboratory, treatment, behavioral, and disease-specific data for a patient. It contains 200+ fields organized by clinical category. All fields are nullable/optional unless noted.

### Core Demographics

| Field | Type | Notes |
|-------|------|-------|
| `id` | int | Auto PK (read-only) |
| `person` | int | FK → Person.person_id (read-only) |
| `email` | string | Max 255 |
| `date_of_birth` | date | `YYYY-MM-DD` |
| `patient_age` | int | |
| `gender` | string(2) | `M` / `F` / `U` |
| `weight` | float | |
| `weight_units` | string(2) | `kg` / `lb` (default `kg`) |
| `height` | float | |
| `height_units` | string(2) | `cm` / `in` (default `cm`) |
| `bmi` | float | Computed, read-only |
| `ethnicity` | string | |
| `systolic_blood_pressure` | int | |
| `diastolic_blood_pressure` | int | |

### Geographic

| Field | Type |
|-------|------|
| `country` | string(255) |
| `region` | string(255) |
| `city` | string(255) |
| `postal_code` | string(20) |
| `longitude` | float |
| `latitude` | float |

### Disease & Staging

| Field | Type | Notes |
|-------|------|-------|
| `disease` | string | Main diagnosis |
| `stage` | string | Disease stage |
| `karnofsky_performance_score` | int | Default 100 |
| `ecog_performance_status` | int | |
| `no_other_active_malignancies` | bool | Default `true` |
| `no_pre_existing_conditions` | bool | |
| `peripheral_neuropathy_grade` | int | |

### Treatment History

Each therapy line has a consistent set of fields:

| Prefix | Meaning |
|--------|---------|
| `first_line_*` | 1st-line therapy |
| `second_line_*` | 2nd-line therapy |
| `later_*` | 3rd+ line therapy |
| `supportive_*` | Supportive care |

**Fields per line:**

| Suffix | Type | Notes |
|--------|------|-------|
| `_therapy` | string | Regimen name |
| `_date` | date | General date |
| `_start_date` | date | Treatment start |
| `_end_date` | date | Treatment end |
| `_outcome` | string | `CR` / `PR` / `SD` / `PD` |
| `_intent` | string(50) | `Adjuvant` / `Neoadjuvant` / `Metastatic` |
| `_discontinuation_reason` | string(50) | `Progression` / `Toxicity` / `Completion` |

**Computed treatment fields:**

| Field | Type | Notes |
|-------|------|-------|
| `prior_therapy` | string | `None` / `One line` / `Two lines` / `More than two lines` |
| `relapse_count` | int | Computed from outcomes |
| `treatment_refractory_status` | string(255) | `Not Refractory` / `Primary Refractory` / `Secondary Refractory` / `Multi-Refractory` / `Unknown` |
| `therapy_lines_count` | int | Computed count |
| `therapy_intent` | string(50) | Overall intent |
| `reason_for_discontinuation` | string(100) | Overall reason |

### Hematology

| Field | Type | Unit |
|-------|------|------|
| `hemoglobin_g_dl` | decimal(5,1) | g/dL |
| `hematocrit_percent` | decimal(5,1) | % |
| `wbc_count_thousand_per_ul` | decimal(6,1) | 10³/µL |
| `rbc_million_per_ul` | decimal(5,2) | 10⁶/µL |
| `platelet_count_thousand_per_ul` | decimal(6,1) | 10³/µL |
| `anc_thousand_per_ul` | decimal(6,1) | 10³/µL |
| `alc_thousand_per_ul` | decimal(6,1) | 10³/µL |
| `amc_thousand_per_ul` | decimal(6,1) | 10³/µL |

### Renal Function

| Field | Type | Unit |
|-------|------|------|
| `serum_calcium_mg_dl` | decimal(5,1) | mg/dL |
| `serum_creatinine_mg_dl` | decimal(5,2) | mg/dL |
| `creatinine_clearance_ml_min` | decimal(6,1) | mL/min |
| `egfr_ml_min_173m2` | decimal(6,1) | mL/min/1.73m² |
| `bun_mg_dl` | decimal(5,1) | mg/dL |

### Electrolytes

| Field | Type | Unit |
|-------|------|------|
| `sodium_meq_l` | decimal(5,1) | mEq/L |
| `potassium_meq_l` | decimal(5,1) | mEq/L |
| `calcium_mg_dl` | decimal(5,1) | mg/dL |
| `magnesium_mg_dl` | decimal(5,1) | mg/dL |
| `magnesium` | decimal(5,1) | mg/dL |
| `phosphorus` | decimal(5,1) | mg/dL |

### Liver Function

| Field | Type | Unit |
|-------|------|------|
| `bilirubin_total_mg_dl` | decimal(5,1) | mg/dL |
| `serum_bilirubin_level_direct` | decimal(10,2) | mg/dL |
| `alt_u_l` | int | U/L |
| `ast_u_l` | int | U/L |
| `alkaline_phosphatase_u_l` | int | U/L |
| `alkaline_phosphatase` | int | U/L |
| `albumin_g_dl` | decimal(5,1) | g/dL |
| `total_protein` | decimal(5,1) | g/dL |

### Cardiac & Metabolic

| Field | Type | Unit |
|-------|------|------|
| `troponin_ng_ml` | decimal(7,3) | ng/mL |
| `bnp_pg_ml` | int | pg/mL |
| `glucose_mg_dl` | int | mg/dL |
| `hba1c_percent` | decimal(4,1) | % |
| `ldh_u_l` | int | U/L |
| `ldh` | int | U/L |

### Coagulation

| Field | Type | Unit |
|-------|------|------|
| `inr` | decimal(5,2) | — |
| `pt_seconds` | decimal(5,1) | seconds |
| `ptt_seconds` | decimal(5,1) | seconds |

### Tumor Markers

| Field | Type | Unit |
|-------|------|------|
| `cea_ng_ml` | decimal(8,1) | ng/mL |
| `ca19_9_u_ml` | decimal(8,1) | U/mL |
| `psa_ng_ml` | decimal(7,2) | ng/mL |

### Behavioral — Lifestyle

| Field | Type | Notes |
|-------|------|-------|
| `smoking_status` | string(50) | `Never` / `Former` / `Current` |
| `pack_years` | decimal(5,1) | |
| `alcohol_use` | string(50) | `None` / `Light` / `Moderate` / `Heavy` |
| `drinks_per_week` | int | |
| `exercise_frequency` | string(50) | |
| `exercise_minutes_per_week` | int | |
| `diet_type` | string(100) | |

### Behavioral — Sleep & Wellbeing

| Field | Type |
|-------|------|
| `sleep_hours_per_night` | decimal(4,1) |
| `sleep_quality` | string(50) |
| `stress_level` | string(50) |
| `social_support` | string(50) |

### Behavioral — Socioeconomic

| Field | Type |
|-------|------|
| `employment_status` | string(50) |
| `education_level` | string(100) |
| `marital_status` | string(50) |
| `insurance_type` | string(100) |
| `number_of_dependents` | int |
| `annual_household_income` | decimal(12,2) |

### Cancer Assessment

| Field | Type | Notes |
|-------|------|-------|
| `ecog_assessment_date` | date | |
| `test_methodology` | string(50) | `NGS` / `IHC` / `FISH` / `PCR` |
| `test_date` | date | |
| `test_specimen_type` | string(50) | `Primary Biopsy` / `Metastatic Biopsy` |
| `report_interpretation` | string(50) | `Positive` / `Negative` / `Indeterminate` / `Not Tested` |
| `oncotype_dx_score` | int | |
| `androgen_receptor_status` | string(50) | |

### Reproductive Health

| Field | Type |
|-------|------|
| `pregnancy_test_date` | date |
| `pregnancy_test_result_value` | string(50) |
| `contraceptive_use` | bool (default `false`) |

### Consent & Support

| Field | Type | Default |
|-------|------|---------|
| `consent_capability` | bool | `true` |
| `caregiver_availability_status` | bool | `false` |

### Mental Health & Substance Use

| Field | Type | Default |
|-------|------|---------|
| `no_mental_health_disorder_status` | bool | `true` |
| `no_substance_use_status` | bool | `true` |
| `substance_use_details` | string(255) | |
| `no_tobacco_use_status` | bool | `true` |
| `tobacco_use_details` | string(255) | |

### Geographic & Infection Risk

| Field | Type | Default |
|-------|------|---------|
| `no_geographic_exposure_risk` | bool | `true` |
| `geographic_exposure_risk_details` | string(255) | |
| `no_hiv_status` | bool | `true` |
| `no_hepatitis_b_status` | bool | `true` |
| `no_hepatitis_c_status` | bool | `true` |
| `no_active_infection_status` | bool | `true` |

### Breast Cancer Specific

| Field | Type | Notes |
|-------|------|-------|
| `tumor_size` | float | cm |
| `lymph_node_status` | string(50) | `Positive` / `Negative` / `Unknown` |
| `metastasis_status` | string(50) | `Positive` / `Negative` / `Unknown` |
| `tumor_stage` | string | TNM T stage |
| `nodes_stage` | string | TNM N stage |
| `distant_metastasis_stage` | string | TNM M stage |
| `staging_modalities` | string | |
| `measurable_disease_by_recist_status` | bool | |
| `bone_only_metastasis_status` | bool | |
| `histologic_type` | string | |
| `biopsy_grade` | int | |
| `estrogen_receptor_status` | string | |
| `progesterone_receptor_status` | string | |
| `her2_status` | string | |
| `tnbc_status` | bool | |
| `hrd_status` | string | |
| `hr_status` | string | |
| `ki67_proliferation_index` | int | % |
| `pd_l1_tumor_cels` | int | % |
| `pd_l1_assay` | string | |
| `pd_l1_ic_percentage` | int | % |
| `pd_l1_combined_positive_score` | int | |

### CLL Specific

| Field | Type |
|-------|------|
| `binet_stage` | string |
| `tp53_disruption` | bool |
| `btk_inhibitor_refractory` | bool |
| `bcl2_inhibitor_refractory` | bool |
| `absolute_lymphocyte_count` | float |
| `clonal_b_lymphocyte_count` | int |
| `clonal_bone_marrow_b_lymphocytes` | float |
| `bone_marrow_involvement` | bool |

### Lymphoma Specific

| Field | Type |
|-------|------|
| `flipi_score` | int |
| `gelf_criteria_status` | string |
| `tumor_grade` | int |

### Genetics

| Field | Type | Notes |
|-------|------|-------|
| `genetic_mutations` | JSON | Array of `{ gene, mutation, origin, interpretation }` |

### Timestamps

| Field | Type | Notes |
|-------|------|-------|
| `created_at` | datetime | Auto, read-only |
| `updated_at` | datetime | Auto, read-only |

---

## 10. Person (OMOP CDM) Schema

The OMOP CDM `Person` table extended with `given_name` and `family_name` fields. Serves as the identity anchor — `PatientInfo` has a one-to-one FK to this table. Gender, race, and ethnicity are stored as OMOP concept references.

| Field | Type | Notes |
|-------|------|-------|
| `person_id` | int | Primary key |
| `gender_concept` | FK → Concept | OMOP gender concept |
| `gender_source_value` | string(50) | Source gender |
| `year_of_birth` | int | |
| `month_of_birth` | int | |
| `day_of_birth` | int | |
| `birth_datetime` | datetime | |
| `race_concept` | FK → Concept | OMOP race concept |
| `race_source_value` | string(50) | |
| `ethnicity_concept` | FK → Concept | OMOP ethnicity concept |
| `ethnicity_source_value` | string(50) | |
| `given_name` | string(100) | Extension field |
| `family_name` | string(100) | Extension field |

---

## 11. Serializer Summary

DRF serializers that shape API request/response payloads. The list serializer is intentionally minimal for performance; the detail serializer exposes every model field plus computed properties (name, age, gender from the related Person).

### `UserSerializer`

Read-only. Fields: `id`, `username`, `email`, `first_name`, `last_name`.

### `PatientListSerializer`

Used by `GET /api/patient-info/` (list view). Lightweight.

| Field | Source |
|-------|--------|
| `id` | PatientInfo PK |
| `person_id` | Person.person_id (read-only) |
| `patient_name` | Computed: `given_name + family_name` |
| `age` | Computed from `date_of_birth` |
| `disease` | PatientInfo.disease |
| `stage` | PatientInfo.stage |
| `updated_at` | Formatted as `YYYY-MM-DD` |

### `PatientInfoSerializer`

Used by detail/create/update views. `fields = '__all__'` plus computed fields.

| Computed Field | Source |
|----------------|--------|
| `patient_name` | `Person.given_name + Person.family_name` |
| `age` | Calculated from `date_of_birth` |
| `gender` | From `Person.gender_concept` |
| `refractory_status` | Alias for `treatment_refractory_status` |

Read-only: `person`, `created_at`, `updated_at`.

---

## 12. FHIR LOINC Code Mapping

Complete reference of LOINC codes recognized by the FHIR upload endpoint. When an `Observation` resource in the uploaded bundle contains one of these codes, its value is extracted and stored in the corresponding `PatientInfo` field. Text-based matching (bottom of this section) is used as a fallback when no LOINC code is present.

### Hematology

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `718-7` | Hemoglobin | `hemoglobin_g_dl` |
| `4544-3` | Hematocrit | `hematocrit_percent` |
| `6690-2` | WBC Count | `wbc_count_thousand_per_ul` |
| `789-8` | RBC Count | `rbc_million_per_ul` |
| `777-3` | Platelet Count | `platelet_count_thousand_per_ul` |
| `751-8` | ANC | `anc_thousand_per_ul` |
| `731-0` | ALC | `alc_thousand_per_ul` |
| `742-7` | AMC | `amc_thousand_per_ul` |

### Renal

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `2160-0` | Creatinine | `serum_creatinine_mg_dl` |
| `2164-2` | Creatinine Clearance | `creatinine_clearance_ml_min` |
| `33914-3` | eGFR | `egfr_ml_min_173m2` |
| `3094-0` | BUN | `bun_mg_dl` |
| `2000-8` | Calcium | `serum_calcium_mg_dl` |

### Electrolytes

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `2951-2` | Sodium | `sodium_meq_l` |
| `2823-3` | Potassium | `potassium_meq_l` |
| `19123-9` | Magnesium | `magnesium_mg_dl` |

### Liver Function

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `1975-2` | Bilirubin Total | `bilirubin_total_mg_dl` |
| `1968-7` | Bilirubin Direct | `serum_bilirubin_level_direct` |
| `1742-6` | ALT | `alt_u_l` |
| `1920-8` | AST | `ast_u_l` |
| `6768-6` | ALP | `alkaline_phosphatase_u_l` |
| `1751-7` | Albumin | `albumin_g_dl` |
| `2885-2` | Total Protein | `total_protein` |

### Tumor Markers

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `2039-6` | CEA | `cea_ng_ml` |
| `25390-6` | CA 19-9 | `ca19_9_u_ml` |
| `2857-1` | PSA | `psa_ng_ml` |

### Cardiac

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `10839-9` | Troponin | `troponin_ng_ml` |
| `42637-9` | BNP | `bnp_pg_ml` |

### Metabolic

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `2345-7` | Glucose | `glucose_mg_dl` |
| `4548-4` | HbA1c | `hba1c_percent` |
| `2532-0` | LDH | `ldh_u_l` |

### Coagulation

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `6301-6` | INR | `inr` |
| `5902-2` | PT | `pt_seconds` |
| `3173-2` | PTT | `ptt_seconds` |

### Oncology & Behavioral

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `72166-2` | Smoking Status | `smoking_status` |
| `63640-7` | Pack Years | `pack_years` |
| `74013-4` | Alcohol Use | `alcohol_use` |
| `89247-1` | ECOG | `ecog_performance_status` |
| `85337-4` | Test Method | `test_methodology` |
| `31208-2` | Specimen | `test_specimen_type` |
| `69548-6` | Interpretation | `report_interpretation` |
| `16112-5` | Androgen Receptor | `androgen_receptor_status` |
| `42804-5` | Therapy Intent | `therapy_intent` |
| `91379-3` | Discontinuation Reason | `reason_for_discontinuation` |

### Reproductive & Consent

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `2106-3` | Pregnancy Test | `pregnancy_test_result_value` |
| `8659-8` | Contraceptive | `contraceptive_use` |
| `75985-6` | Consent | `consent_capability` |
| `74014-2` | Caregiver | `caregiver_availability_status` |

### Mental Health & Risk

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `75618-3` | Mental Health | `no_mental_health_disorder_status` |
| `74204-0` | Substance Use | `no_substance_use_status` |
| `82593-5` | Geographic Risk | `no_geographic_exposure_risk` |

### TNM Staging

| LOINC | Display | Target Field |
|-------|---------|-------------|
| `21905-5` | T Stage | `tumor_stage` |
| `21906-3` | N Stage | `nodes_stage` |
| `21901-4` | M Stage | `distant_metastasis_stage` |
| `85319-2` | TNM Staging Method | `staging_modalities` |

### Text-Based Matching (code.text contains)

| Text Pattern | Target Field |
|-------------|-------------|
| `tumor size` | `tumor_size` |
| `lymph node` | `lymph_node_status` |
| `metastasis` | `metastasis_status` |
| `ER` / `estrogen` | `estrogen_receptor_status` |
| `PR` / `progesterone` | `progesterone_receptor_status` |
| `HER2` | `her2_status` |
| `Ki67` / `Ki-67` | `ki67_proliferation_index` |
| `PD-L1` | `pd_l1_tumor_cels` |

---

## 13. Frontend Integration Notes

How the React/TypeScript SPA connects to the API. The frontend uses Axios with automatic CSRF token injection and a 401-interceptor that redirects to the login page when the session expires.

### Axios Configuration

- Base URL: `/api` (relative, proxied by the dev server or same-origin in production)
- `withCredentials: true` for session cookie forwarding
- Request interceptor extracts `csrftoken` cookie and sets `X-CSRFToken` header
- Response interceptor catches `401` and redirects to `/login`

### URL Routing (Frontend → Backend)

| Frontend Action | API Call |
|-----------------|----------|
| Login | `POST /api/auth/login/` |
| Logout | `POST /api/auth/logout/` |
| Load patient list | `GET /api/patient-info/` |
| Load patient detail | `GET /api/patient-info/{person_id}/` |
| Save patient edits | `PATCH /api/patient-info/{person_id}/` |
| Upload CSV | `POST /api/patient-info/upload_csv/` |
| Upload FHIR | `POST /api/patient-info/upload_fhir/` |
| Bulk delete | `DELETE /api/patient-info/bulk_delete/` |

---

## Endpoint Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/auth/login/` | No | Authenticate with username/password; returns session cookie and user profile |
| `POST` | `/api/auth/logout/` | No | Invalidate session and log out |
| `POST` | `/api/auth/test/` | No | Debug endpoint returning current auth/session state |
| `GET` | `/api/health/` | No | Liveness probe — confirms service and database are up |
| `GET` | `/api/user/` | Yes | Return the authenticated user's profile |
| `GET` | `/api/patient-info/` | Yes | List all patients (summary: name, age, disease, stage) |
| `POST` | `/api/patient-info/` | Yes | Create a new patient record |
| `GET` | `/api/patient-info/{id}/` | Yes | Retrieve full clinical record (200+ fields) for one patient |
| `PUT` | `/api/patient-info/{id}/` | Yes | Replace entire patient record |
| `PATCH` | `/api/patient-info/{id}/` | Yes | Partial update — modify specific fields only |
| `PATCH` | `/api/patient-info/{id}/update_patient/` | Yes | Named partial-update action (equivalent to PATCH on detail) |
| `POST` | `/api/patient-info/upload_csv/` | Yes | Bulk-create patients from a CSV file |
| `POST` | `/api/patient-info/upload_fhir/` | No | Ingest a FHIR R4 Bundle; maps 60+ LOINC codes to patient fields |
| `DELETE` | `/api/patient-info/bulk_delete/` | Yes | Delete multiple patients by person_id array; cascades to Person + User |
