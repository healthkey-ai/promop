# PatientInfo ↔ OMOP Bidirectional Sync — Design Spec

**Date:** 2026-05-16  
**Status:** Approved  
**Related issues:** #31 (OMOP write APIs), #67 (LOT inference)

---

## Problem

`PatientInfo` is a denormalized flat table that currently acts as both a write target (UI PATCH) and a read source. Only ~55 lab/vital fields write through to `Measurement` when PATCHed. Disease staging, therapy lines, and demographics have no write-through at all. Meanwhile, FHIR imports write directly to OMOP tables (`DrugExposure`, `Measurement`, `ConditionOccurrence`, `ProcedureOccurrence`) but those records are not reflected back into `PatientInfo`.

The result: `PatientInfo` and OMOP tables drift out of sync depending on which path wrote the data.

---

## Architecture

Two separate services handle each sync direction. A shared mappings module eliminates duplication.

```
UI PATCH ──► PatientInfo ──► omop_write_service ──► OMOP tables
                                                          │
FHIR import ──────────────────────────────────────► OMOP tables
                                                          │
                                              LOT inference (#67)
                                                          │
                                    omop_read_service ◄───┘
                                    (patient_info_service.py)
                                          │
                                          ▼
                                     PatientInfo
```

### Files

| File | Role |
|---|---|
| `omop_core/services/mappings.py` | Shared field→LOINC/concept mappings (moved from views.py) |
| `omop_core/services/omop_write_service.py` | **New.** PatientInfo → OMOP write-through |
| `omop_core/services/patient_info_service.py` | **Existing.** OMOP → PatientInfo read/refresh |

---

## omop_write_service.py

### Entry point

```python
def sync_to_omop(patient_info: PatientInfo, changed_fields: set[str], today: date = None) -> None:
    """
    Called after any PatientInfo save. Writes changed fields through to the
    appropriate OMOP tables. Never raises — failures are logged but do not
    block the caller.
    """
```

Called from `PatientInfoViewSet.partial_update` after `patient_info.save()`. The `changed_fields` set is derived from `request.data.keys()`.

### Measurement (labs/vitals)

- **Trigger:** any field in `LAB_FIELD_TO_LOINC` (from `mappings.py`)
- **Behavior:** upsert by `(person, loinc_code, measurement_date=today)` — same-day updates overwrite a single row; a different day always creates a new row, preserving longitudinal history
- **Source:** moves `_upsert_omop_measurement` and `_LAB_FIELD_TO_LOINC` from `views.py` into this service and `mappings.py` respectively

### ConditionOccurrence (disease/staging)

- **Trigger:** changes to `disease`, `stage`, `condition_code_icd_10`, `condition_code_snomed_ct`
- **Behavior:** **append** — always insert a new `ConditionOccurrence` with `condition_start_date = today`
- **Concept lookup:** `Concept.objects.filter(concept_name__icontains=disease_name).first()`, fallback `concept_id=0`
- `condition_source_value` = disease name or stage string

### Person (demographics)

- **Trigger:** changes to `gender`, `date_of_birth`, `patient_age`, `ethnicity`
- **Behavior:** **upsert** — update the existing `Person` record in place
- Maps `date_of_birth` → `year_of_birth`, `month_of_birth`, `day_of_birth`
- Maps `gender` → `gender_concept_id` via existing `get_gender_concept()` in views.py (move to mappings.py)

### Episode + EpisodeEvent (therapy lines)

Handles `first_line_*`, `second_line_*`, and `later_*` field groups (line numbers 1, 2, 3 respectively).

**Episode upsert:**
- Match on `(person, episode_number, episode_start_datetime = line_start_date)` where `line_start_date` is not null
- If `line_start_date` is null: match on `(person, episode_number)` alone — update the most recent episode for that line number, or create one with `episode_start_datetime = today` as a placeholder
- If found: update `episode_end_datetime`, `episode_source_value` (regimen name)
- If not found: create new `Episode` with `episode_concept_id=32531` (Treatment Regimen)
- `episode_source_value` = therapy name (e.g. "AC-T")

**EpisodeEvent linking (after Episode upsert):**
- Find `DrugExposure` rows for this person where `drug_exposure_start_date` falls within the episode's date range
- Find `ProcedureOccurrence` rows similarly
- For each, check whether an `EpisodeEvent` already links it to this Episode
- If not already linked: create `EpisodeEvent(episode=episode, event_id=record_id, episode_event_field_concept_id=1147094)` — concept 1147094 = `drug_exposure_id` field; use equivalent concept for `ProcedureOccurrence`
- **Do not create DrugExposure rows** — those come from FHIR import only

---

## mappings.py

Moves out of `views.py`:
- `LAB_FIELD_TO_LOINC` dict (55 fields → LOINC code, unit, display name)
- `get_gender_concept(gender_str)` helper
- Any other field→concept mappings needed by both services

---

## patient_info_service.py (existing — read direction)

No structural changes. Continues to handle OMOP → PatientInfo refresh:
- Called after FHIR import completes
- Called after LOT inference (#67) sets Episode records
- Reads latest `Measurement` rows → populates lab fields
- Reads `ConditionOccurrence` → populates `disease`, `stage`
- Reads `Episode` + `EpisodeEvent` → populates `first/second/later_line_therapy`
- Reads `Person` → populates demographics

---

## Relation to LOT Inference (#67)

LOT inference is a separate pipeline, not part of either sync service:

1. FHIR import → raw `DrugExposure` + `ProcedureOccurrence` rows
2. LOT inference (#67) — reads those rows, applies OHDSI Artemis + custom rules, creates named `Episode` records with `episode_number` and `episode_source_value` (regimen name), creates `EpisodeEvent` links
3. `patient_info_service.py` refresh — reads the new `Episode` records, writes `first/second/later_line_therapy` into `PatientInfo`

The `omop_write_service` handles the reverse: if a human manually corrects the named therapy in `PatientInfo` via the UI, that correction propagates back to the `Episode` record.

---

## views.py changes

`PatientInfoViewSet.partial_update` becomes:

```python
def partial_update(self, request, pk=None):
    # ... existing auth / org-scoping checks ...
    patient_info.save()
    sync_to_omop(patient_info, changed_fields=set(request.data.keys()), today=today)
    return Response(serializer.data)
```

Remove from `views.py`: `_LAB_FIELD_TO_LOINC`, `_upsert_omop_measurement`.

---

## Error handling

`sync_to_omop` wraps all OMOP writes in a broad `try/except`. A logging failure or DB error must never raise to the caller — the PatientInfo save has already succeeded and the HTTP response must return. Failures are logged to the `audit` logger with `event=omop_sync_error`.

---

## Tests

All tests run against dev PostgreSQL. New test class: `PatientInfoOmopSyncTest(_SmartBase)`.

| Test | Assertion |
|---|---|
| `test_patch_lab_creates_measurement` | PATCH hemoglobin → new Measurement row |
| `test_patch_lab_same_day_updates_not_duplicates` | Two PATCHes same day → still 1 Measurement row |
| `test_patch_lab_different_day_appends` | PATCH on day 1, PATCH on day 2 → 2 Measurement rows |
| `test_patch_disease_creates_condition_occurrence` | PATCH disease → new ConditionOccurrence |
| `test_patch_stage_appends_condition_occurrence` | Two stage PATCHes → 2 ConditionOccurrence rows |
| `test_patch_demographics_updates_person` | PATCH gender/dob → Person record updated |
| `test_patch_first_line_therapy_creates_episode` | PATCH first_line_therapy → Episode(episode_number=1) |
| `test_patch_therapy_links_existing_drug_exposures` | Existing DrugExposure in date range → EpisodeEvent created |
| `test_patch_therapy_no_duplicate_episode_events` | Repeat PATCH → EpisodeEvent not duplicated |
| `test_sync_failure_does_not_block_response` | DB error in sync → PATCH still returns 200 |
| `test_lab_field_to_loinc_in_mappings_not_views` | `_LAB_FIELD_TO_LOINC` not importable from views |
