# Follicular Lymphoma (FL) — Architecture As-Implemented

Reference for Vlad's FLF-PHR implementation. Enumerates everything built to
support FL across data model, derivation, API, FHIR, frontend, and therapy
management.

---

## 1. PatientRecord Fields (FL-Specific)

All on `omop_core.models.PatientRecord`, nullable unless noted.

### Disease Staging & Grading

| Field | Type | Source | Notes |
|---|---|---|---|
| `stage` | TextField | FHIR Condition stage, OMOP, UI | Ann Arbor I/II/III/IV with optional B-suffix |
| `tumor_grade` | CharField(10) | FHIR extension, UI | 1, 2, 3A, 3B. Normalized by `flipi.normalize_grade()` |

### FLIPI-1 (Five-Factor Prognostic Index)

| Field | Type | Source | Notes |
|---|---|---|---|
| `flipi_score` | IntegerField | **Derived** from `flipi_score_options` | 0–5 |
| `flipi_score_options` | TextField | User input, FHIR, sample profiles | Comma-separated factor keys: `age`, `stage`, `hemoglobin`, `nodalAreas`, `ldh` |
| `flipi_risk_category` | CharField(20) | **Derived** from score | `Low` (0–1), `Intermediate` (2), `High` (3–5) |

Service: `omop_core/services/flipi.py` — `parse_factors()`, `calculate_flipi()`,
`normalize_grade()`. Accepts aliases (e.g. "age >= 60", "elevated ldh").

### GELF Criteria

| Field | Type | Source | Notes |
|---|---|---|---|
| `gelf_criteria_status` | TextField | User input, FHIR | "Meets GELF criteria" / "does not meet GELF criteria" |
| `gelf_criteria_options` | TextField | User input, FHIR | Comma-separated factors (bulky mass, B-symptoms, splenomegaly, cytopenia, etc.) |

### Disease Burden

| Field | Type | Notes |
|---|---|---|
| `number_of_nodal_sites` | PositiveSmallIntegerField | Count of involved nodal areas |
| `bulky_disease` | BooleanField | Nodal/extranodal mass >7 cm |
| `bone_marrow_involvement` | BooleanField | BM biopsy involvement |

### B-Symptoms (Three Dated Observations)

Replaced the legacy single `b_symptoms` boolean (PRs #2072, migration 0313/0314).

| Field | Type | SNOMED | Notes |
|---|---|---|---|
| `b_symptom_fever` | BooleanField | 386661006 | Unexplained fever >38 C |
| `b_symptom_night_sweats` | BooleanField | 42984000 | Drenching night sweats |
| `b_symptom_weight_loss` | BooleanField | 262285001 | Weight loss >10% in 6 months |
| `b_symptoms` | BooleanField | — | **Derived** composite: True if any individual True |

### Transformation to DLBCL

| Field | Type | Notes |
|---|---|---|
| `transformed_to_dlbcl` | BooleanField | **Derived** when DLBCL ConditionOccurrence exists alongside FL |
| `dlbcl_transformation_date` | DateField | From DLBCL condition onset |
| `post_transformation_outcome` | TextField | Vocabulary: `PostTransformationOutcome` |

### Progression & POD24 (Slice 5, PR #2097)

| Field | Type | Notes |
|---|---|---|
| `first_line_progression_date` | DateField | From `LOT-1-progression` Observation |
| `second_line_progression_date` | DateField | From `LOT-2-progression` Observation |
| `later_progression_date` | DateField | Earliest LOT-N-progression, N >= 3 |
| `pod24` | BooleanField | **Derived**: True if `first_line_progression_date - first_line_start_date <= 730 days` |

### Other Lab References

| Field | Type | Notes |
|---|---|---|
| `ldh_upper_limit_normal` | PositiveIntegerField | Lab's ULN for LDH (typically 225 U/L) |

---

## 2. Vocabulary Tables

| Model | DB table | Contents |
|---|---|---|
| `FlipIScore` | `vocabulary_flipi_score` | FLIPI factor standardization |
| `FollicularLymphomaGrade` | `vocabulary_follicular_lymphoma_grade` | Grade values: 1, 2, 3A, 3B |
| `PostTransformationOutcome` | `vocabulary_post_transformation_outcome` | Outcomes after DLBCL transformation |

Served via `/api/vocabularies/<slug>/`.

---

## 3. Maintenance Therapy (Slice 5, PR #2097)

### Data Model

`omop_oncology.models.TherapyLineExposure` has `is_maintenance` (BooleanField,
default False). This is a per-drug, per-line flag — not a separate Episode.

### How It Flows

1. **FHIR generator** (`_fl_generator.py::_maintenance_rituximab`) emits a
   MedicationStatement with extension `fl-maintenance: true` and `partOf`
   referencing the first-line regimen.
2. **FHIR upload** parses the extension and sets `is_maintenance=True` on the
   `TherapyLineExposure` row.
3. **Editor API** (`POST/PATCH /v1/therapy-lines/`) accepts `maintenance: true`
   or `false` on each drug dict in the `drugs` array. Omission preserves the
   existing flag, including when an older client echoes the drugs list.
4. **Import API** (`POST /v1/therapy-lines/bulk_upsert/`) accepts
   `maintenance_drugs` per line, marking matching existing line exposures
   without replacing the line's drug composition.
5. **Derivation** reads `TherapyLineExposure.is_maintenance` and includes
   `has_maintenance` and `maintenance_drugs` in the `later_therapies` JSON
   (structured `lines_of_therapy` payload).
6. **Frontend** shows a per-drug "Maintenance" checkbox in the
   `TherapyLineDialog`.

---

## 4. Progression Dates & POD24 (Slice 5, PR #2097)

### Storage

`LOT-{n}-progression` Observations on OMOP `Observation`:
- `observation_source_value = 'LOT-1-progression'`
- `observation_date = <progression date>`
- `value_as_string = 'Progressive Disease'`

Created by `episode_service.upsert_therapy_line_episode(progression_date=...)`.

### Derivation

`patient_record_service._get_treatment_data_from_episodes()`:
1. Collects all `LOT-N-progression` Observations, grouped by line number.
2. Sets `first_line_progression_date`, `second_line_progression_date`,
   `later_progression_date` on PatientRecord. The flat later field is a summary;
   each `later_therapies` entry carries its own `progression_date`, which the
   editable `lines_of_therapy` payload uses. A sibling line's date is never an
   editable default for a line with no progression recorded.
3. Computes POD24: `(first_prog - first_start).days <= 730`.

### API Entry Points

| Endpoint | Parameter | Notes |
|---|---|---|
| `POST /v1/therapy-lines/` | `progression_date` in payload | Editor API |
| `PATCH /v1/therapy-lines/{id}/` | `progression_date` in payload | Editor API |
| `POST /v1/therapy-lines/bulk_upsert/` | `progression_date` per line | Import API |

Validation: future dates rejected; progression before start rejected.

### Frontend

- Date picker in `TherapyLineDialog.tsx`.
- Progression date shown inline on each therapy line in `TreatmentTab.tsx`.
- POD24 badge (red "POD24 positive" / green "POD24 negative") on the treatment
  tab when `formData.pod24 != null`.

---

## 5. FHIR Bundle Generator

**Command**: `manage.py generate_fhir_bundle --disease fl --count N --output <path>`

**Class**: `FLBundleGenerator` in `omop_core/management/commands/_fl_generator.py`

### Patient Profile (`_profile()`)

Generates realistic FL characteristics:
- Ann Arbor stage (weighted I–IV)
- Tumor grade (1/2/3A/3B)
- FLIPI score from five factors (age, stage, hemoglobin, nodal sites, LDH)
- GELF assessment (bulky disease, B-symptoms, cytopenia, splenomegaly)
- Watch-and-wait eligibility (~20%; no GELF, no B-symptoms, grade <= 2)
- Maintenance rituximab eligibility (~50% of treated)
- DLBCL transformation risk (~4% per line, capped 25%)
- POD24 simulation (~20% of multi-line patients)

### Therapy Timeline (`_build_therapy_timeline()`)

- Multi-year lines with progression-free intervals (1L median ~6 years)
- Regimen selection from `FL_LOT_WEIGHTS` (see below)
- Per-line outcomes weighted by line number
- Maintenance rituximab for eligible patients (180–300 day start, 540–730 day
  duration post-diagnosis)

### FHIR Extensions on Patient

| Extension URL suffix | Maps to | Type |
|---|---|---|
| `fl-tumor-grade` | `tumor_grade` | valueInteger |
| `fl-flipi-score` | `flipi_score` | valueInteger |
| `fl-flipi-risk-category` | `flipi_risk_category` | valueString |
| `fl-gelf-criteria` | `gelf_criteria_status` | valueString |
| `fl-bone-marrow-involvement` | `bone_marrow_involvement` | valueBoolean |
| `fl-b-symptoms` | `b_symptoms` (legacy) | valueBoolean |
| `fl-bulky-disease` | `bulky_disease` | valueBoolean |
| `fl-nodal-sites` | `number_of_nodal_sites` | valueInteger |
| `fl-watch-and-wait` | (metadata only) | valueBoolean |
| `fl-transformed` | `transformed_to_dlbcl` | valueBoolean |
| `fl-maintenance` | `TherapyLineExposure.is_maintenance` | valueBoolean (on MedicationStatement) |

### Condition Resources

1. **Follicular Lymphoma** — SNOMED 413448000, ICD-10 C82.90, with Ann Arbor
   staging and condition notes (FLIPI, GELF, nodal count, BM status).
2. **DLBCL** (if transformed) — separate active Condition with its own onset date.

### Therapy Resources

MedicationStatements per drug with RxNorm coding, HemOnc Regimen coding on
the parent statement, `therapy-line` and `therapy-outcome` extensions.
Maintenance rituximab has `fl-maintenance: true` and `partOf` reference to 1L.

---

## 6. FHIR Upload Parsing

In `patient_portal/api/views.py::upload_fhir_bundle()`:

### Patient Extensions

All `fl-*` extensions are parsed and mapped to PatientRecord fields (see table
in section 5 above). `fl-b-symptoms` maps to the legacy boolean; the three
individual B-symptom Observations are the primary path.

### Condition Recognition

FL: SNOMED 413448000, ICD-10 C82.*, or text match "follicular lymphoma".
DLBCL: SNOMED C83.30 family. Both present → `transformed_to_dlbcl=True`.

### B-Symptom Observations

| SNOMED | Field |
|---|---|
| 386661006 | `b_symptom_fever` |
| 42984000 | `b_symptom_night_sweats` |
| 262285001 | `b_symptom_weight_loss` |

Composite `b_symptoms` is **derived**, not written directly from FHIR.

---

## 7. Derivation Pipeline

`omop_core/services/patient_record_service.py`

### FLIPI Recalculation

In `recompute_patient_record_fields()`:
```
flipi_score, flipi_risk_category ← calculate_flipi(flipi_score_options)
```

### B-Symptoms Composite

```
b_symptoms = b_symptom_fever OR b_symptom_night_sweats OR b_symptom_weight_loss
```
All False → False. All None → None (unassessed).

### DLBCL Transformation

Queries for DLBCL ConditionOccurrence alongside FL Condition. Sets
`transformed_to_dlbcl=True` and `dlbcl_transformation_date`.

### Progression Dates + POD24

See section 4 above.

### Observation Projection

B-symptom Observations (SNOMED codes above) project to the three individual
fields. `LOT-N-progression` Observations project to the per-line progression
date fields.

---

## 8. Regimen Catalog

`omop_core/services/lot_regimens.py`

### FL Condition Concept

`FL_CONDITION_CONCEPT_ID = 42542169` (HemOnc).

### FL_LOT_WEIGHTS

First-line and later-line weights for regimen selection:

| Regimen | 1L weight | Later weight |
|---|---|---|
| BR (Bendamustine+Rituximab) | 32 | 18 |
| R-CHOP | 24 | 12 |
| R-CVP | 10 | — |
| G-CHOP | 8 | — |
| Rituximab mono | 5 | 4 |
| Obinutuzumab mono | 2 | 2 |
| R2 (Lenalidomide+Rituximab) | — | 12 |
| Tazemetostat | — | 10 |
| Mosunetuzumab | — | 8 |
| Axi-cel (CAR-T) | — | 7 |
| Glofitamab | — | 6 |
| Epcoritamab | — | 5 |
| Copanlisib | — | 5 |
| Tisagenlecleucel (CAR-T) | — | 4 |
| R-GDP, R-GemOx | — | 4 each |

Loaded from HemOnc ConceptRelationship ("Curr adult indic for" → FL condition).

---

## 9. Frontend Components

### DiseaseTab (Lymphoma Section)

`frontend/src/components/PatientInfo/tabs/DiseaseTab.tsx`

Fields rendered for lymphoma disease type:
- Ann Arbor Stage (select)
- Tumor Grade (select: 1, 2, 3A, 3B)
- GELF Assessment component
- FLIPI Assessment component
- Bulky Disease (yes/no)
- B-Symptom fields (fever, night sweats, weight loss — individual yes/no)
- Bone Marrow Involvement
- Number of Nodal Sites
- LDH Upper Limit of Normal
- Transformed to DLBCL + date + post-transformation outcome

### FlipiAssessment Component

Interactive calculator with five checkboxes (age > 60, stage III/IV,
hemoglobin < 12, > 4 nodal areas, LDH elevated). Auto-calculates score and
risk category. Saves factor keys to `flipi_score_options`.

### GelfAssessment Component

Interactive calculator with GELF factor checkboxes. Saves to
`gelf_criteria_options`. Displays derived `gelf_criteria_status`.

### TherapyLineDialog

`frontend/src/components/PatientInfo/TherapyLineDialog.tsx`

- Progression date picker (date input, max today)
- Per-drug "Maintenance" checkbox in the drug list
- Both included in the submit payload

### TreatmentTab

`frontend/src/components/PatientInfo/tabs/TreatmentTab.tsx`

- Progression date shown inline on each line (`Progressed: <date>`)
- POD24 badge: red "POD24 positive" / green "POD24 negative"

### TypeScript Types

`frontend/src/types/patient.ts` — all FL fields on `PatientInfo` interface.
`frontend/src/api/therapyLines.ts` — `TherapyLineDrug.maintenance`,
`TherapyLinePayload.progression_date`, `EditableTherapyLine.progression_date`.

---

## 10. Sample Disease Profiles

`omop_core/services/sample_disease_profiles.py`

When disease = "FL", `profile_values()` fills gaps with realistic random data:
- Tumor grade (weighted 1/2/3A)
- Labs (hemoglobin, LDH, platelets, ANC, beta-2 microglobulin)
- Disease burden (nodal sites, bulky disease, bone marrow involvement)
- FLIPI score options (inferred from age, stage, labs)
- GELF criteria (inferred from bulky disease, B-symptoms, cytopenia)

---

## 11. Tests

| File | Coverage |
|---|---|
| `tests/test_lot_progression.py` | Progression observations, POD24 (positive/negative/null), maintenance flag, import path, validation |
| `tests/test_generate_import_enrich_synthea_fl.py` | FL FHIR generation pipeline |
| `tests/test_therapy_line_import_1969.py` | Therapy line import (includes progression_date) |
| `tests/test_episode_service.py` | Episode service authoring (regression) |
| `tests/test_therapy_line_assertion.py` | Line assertion (regression) |

---

## 12. Architecture Diagram

```
┌──────────────────── Frontend (React/TypeScript) ────────────────────┐
│  DiseaseTab  ─── FlipiAssessment, GelfAssessment, B-Symptoms       │
│  TreatmentTab ── TherapyLineDialog (progression, maintenance)      │
│                  POD24 badge                                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ REST API
┌──────────────────── API (DRF) ──────────────────────────────────────┐
│  PatientRecordViewSet  (GET/PATCH patient-records)                  │
│  TherapyLineViewSet    (POST/PATCH therapy-lines)                  │
│  TherapyLineImportView (POST therapy-lines/bulk_upsert)            │
│  FHIR Upload Handler   (POST fhir/upload)                          │
│  Serializers: TherapyLineWriteSerializer (progression_date)        │
│  lines_of_therapy()    (structured per-line JSON)                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────── Derivation Pipeline ────────────────────────────┐
│  refresh_patient_record()                                          │
│  ├─ FLIPI: flipi_score, flipi_risk_category ← flipi_score_options │
│  ├─ B-symptoms: composite ← individual observations               │
│  ├─ POD24: ← progression_date vs start_date                       │
│  ├─ DLBCL transformation ← dual Condition detection                │
│  └─ Progression dates ← LOT-N-progression Observations            │
└──────────────────────────────┬──────────────────────────────────────┘
                               │
┌──────────────────── OMOP CDM (PostgreSQL) ──────────────────────────┐
│  PatientRecord (read model — denormalized projection)              │
│  Episode + EpisodeEvent (therapy line grouping)                    │
│  TherapyLineExposure (is_maintenance flag)                         │
│  Observation (B-symptoms, FLIPI, progression, LOT-N-*)             │
│  ConditionOccurrence (FL + DLBCL diagnoses)                        │
│  DrugExposure (individual drugs per line)                          │
│  Measurement (labs: hemoglobin, LDH, etc.)                         │
│  Vocabulary tables (FLIPI, grade, outcomes)                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 13. Key Files

| Concern | File(s) |
|---|---|
| FL PatientRecord fields | `omop_core/models.py` (lines ~3882–3907, ~3443–3453) |
| FLIPI service | `omop_core/services/flipi.py` |
| FL FHIR generator | `omop_core/management/commands/_fl_generator.py` |
| FHIR upload (FL extensions) | `patient_portal/api/views.py` (`upload_fhir_bundle`) |
| Derivation (B-symptoms, FLIPI, POD24, DLBCL) | `omop_core/services/patient_record_service.py` |
| Regimen catalog | `omop_core/services/lot_regimens.py` |
| Sample profiles | `omop_core/services/sample_disease_profiles.py` |
| Episode service (progression, maintenance) | `omop_core/services/episode_service.py` |
| Lines-of-therapy builder | `omop_core/api/lines.py` |
| Editor API serializer | `omop_core/api/serializers.py` |
| Editor API view | `omop_core/api/views.py` |
| Import API | `patient_portal/api/therapy_line_import.py` |
| Frontend disease tab | `frontend/src/components/PatientInfo/tabs/DiseaseTab.tsx` |
| Frontend treatment tab | `frontend/src/components/PatientInfo/tabs/TreatmentTab.tsx` |
| Frontend therapy dialog | `frontend/src/components/PatientInfo/TherapyLineDialog.tsx` |
| Frontend types | `frontend/src/types/patient.ts`, `frontend/src/api/therapyLines.ts` |
| Tests | `tests/test_lot_progression.py` |
