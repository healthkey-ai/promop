# Plan: Source Codes on Patient — Per-Patient Mapping & OMOP Generation

## Context

The ETL pipeline sends FHIR bundles that create clinical rows (Measurement, ConditionOccurrence, etc.) via `resolve_source_code()`. Unmapped source codes land as HK-\* quarantine concepts or concept_id=0 in clinical rows, with `proposed` entries in `SourceCodeConceptMapping` (SCCM). Today, curators must navigate to the global Code Mappings page to find and map these codes — there is no patient-centric view.

This feature adds a **Source Codes tab** to the patient detail page, letting curators see which source codes a patient has, map them in place, and **Generate OMOP** (re-resolve clinical rows to approved concepts). The same operation is available as a bulk action on the patient list.

## Data Model: Query-Based, No New Model

"Source codes on a patient" are **derived** from the patient's existing clinical rows — no new junction model. The five clinical tables (Measurement, ConditionOccurrence, DrugExposure, Observation, ProcedureOccurrence) each store a `*_source_value` and `*_concept_id`. We aggregate distinct `(source_value, concept_id)` per table, then left-join SCCM to get mapping status.

FHIR sync already creates both clinical rows and SCCM entries. The Source Codes tab is a **view** over data that already exists.

## Issues (5 total, implement in order)

### Issue 1: Backend — Patient Source Codes Endpoint

`GET /api/v1/patient-records/{person_id}/source-codes/`

**Response:**
```json
{
  "person_id": 12345,
  "source_codes": [
    {
      "source_value": "GLU",
      "omop_table": "measurement",
      "concept_id": 0,
      "concept_name": null,
      "row_count": 3,
      "mapping_id": 42,
      "mapping_status": "proposed",
      "mapping_target_concept_id": null,
      "mapping_target_concept_name": null,
      "source_vocabulary_id": "",
      "source_code": "GLU"
    }
  ],
  "summary": { "total": 15, "unmapped": 4, "proposed": 3, "approved": 8 }
}
```

**Implementation:**
- Add `@action(detail=True, methods=['get'], url_path='source-codes')` to `PatientRecordViewSet` in `patient_portal/api/views.py`
- Permission: admin/staff only (same gate as existing `omop` action, `canViewOmop`)
- For each table in `CLINICAL_TABLES` (`code_resolution.py:73`):
  - `Model.objects.filter(person=person).exclude(source_value__in=['', None]).values(source_col, concept_col).annotate(row_count=Count('pk'))`
- Left-join SCCM: `SourceCodeConceptMapping.objects.filter(source_code__iexact=sv, omop_table=table).select_related('target_concept').first()`
- Resolve concept names via `_resolve_concept_names()` (existing helper)
- Sort: unmapped first, then by occurrence count descending

**Tests** (in `patient_portal/tests.py`):
- `test_source_codes_returns_correct_aggregation` — multiple rows with same source_value → grouped with count
- `test_source_codes_joins_mapping_status` — approved/proposed/unmapped statuses
- `test_source_codes_across_tables` — codes from measurement + condition both appear
- `test_source_codes_admin_only` — patient user gets 403
- `test_source_codes_empty_patient` — no clinical rows → empty list

### Issue 2: Backend — Per-Patient Generate OMOP Endpoint

`POST /api/v1/patient-records/{person_id}/resolve-source-codes/`

Re-resolves clinical rows where `concept_id = 0` or `concept_id` is an HK-\* quarantine concept, using current approved SCCM mappings. Follows the same resolution logic as `repoint_resolvable_zeros` (`omop_core/management/commands/repoint_resolvable_zeros.py`) but scoped to one person.

**Request body (optional):**
```json
{ "source_values": ["GLU", "HGB"], "omop_tables": ["measurement"] }
```
Omitting filters resolves all unresolved codes on all tables.

**Response:** `200 { "resolved": 5, "skipped": 2, "already_resolved": 8 }`
Synchronous for single patient (fast — O(distinct source values) queries, not O(rows)).

**Implementation:**
- New function `resolve_person_source_codes(person, source_values=None, omop_tables=None)` in `omop_core/mapping/code_resolution.py`, after `repoint_clinical_rows` (~line 674)
- Uses `CLINICAL_TABLES`, `_QUARANTINE_VOCABULARIES`, `NO_MATCHING_CONCEPT_ID`, and `approved_mapping_for()` — all existing
- For each table: filter person's rows where concept is 0 or in `_QUARANTINE_VOCABULARIES`, group by source_value
- For each group: call `approved_mapping_for(source_vocabulary_id, source_value)` — if approved, bulk-update concept column
- After all updates: `PatientRecord.objects.filter(person=person).update(derivation_version=0)` + `refresh_patient_record(person)`
- Idempotent: if concept already matches approved mapping, no update
- Add `@action(detail=True, methods=['post'], url_path='resolve-source-codes')` to `PatientRecordViewSet`

**Tests:**
- `test_resolve_updates_zero_concept` — concept_id=0 row updated to approved mapping's target
- `test_resolve_updates_quarantine_concept` — HK-\* concept row updated
- `test_resolve_skips_already_resolved` — row with correct approved concept unchanged
- `test_resolve_skips_no_mapping` — no approved mapping → row left at 0
- `test_resolve_idempotent` — calling twice produces same result, no duplicates
- `test_resolve_filter_by_source_value` — only specified source_values resolved
- `test_resolve_marks_patient_record_stale` — derivation_version set to 0

### Issue 3: Backend — Bulk Generate OMOP for Multiple Patients

`POST /api/v1/patient-records/bulk-resolve-source-codes/`

**Request:** `{ "person_ids": [123, 456, 789] }`
**Response:** `202 { "run_id": "uuid", "total": 3 }`

**Poll:** `GET /api/v1/resolve-runs/{run_id}/`
**Response:** `{ "run_id": "uuid", "state": "running", "total": 3, "done": 1, "resolved": 5, "errors": 0 }`

**Implementation:**
- New model `SourceCodeResolveRun` in `omop_core/models.py` (mirrors `SuggestRun` pattern):
  - `run_id` (UUID PK), `state` (pending/running/completed/failed), `total`, `done`, `resolved`, `errors`, `created_at`, `created_by`
- New Celery task `resolve_bulk_source_codes_task` following `derivation_jobs.py` dispatcher pattern (Celery when broker configured, inline otherwise)
- Max 500 person_ids per request (413 if exceeded)
- Each person processed independently; errors don't stop the batch
- Migration for `SourceCodeResolveRun` model
- Register in `v1_urls.py`: `bulk-resolve-source-codes/` and `resolve-runs/<uuid:run_id>/`

**Tests:**
- `test_bulk_resolve_creates_run` — returns 202 with run_id
- `test_bulk_resolve_progress` — poll shows progress
- `test_bulk_resolve_max_limit` — >500 ids → 413
- `test_bulk_resolve_resolves_across_patients` — two patients each get resolved

### Issue 4: Frontend — Source Codes Tab on Patient Detail

New tab "Source Codes" on patient detail page, visible to admin/staff users, positioned before "OMOP" tab.

**New component:** `frontend/src/components/Patient/PatientSourceCodesTab.tsx`

**Layout:**
- Summary banner: "15 source codes — 4 unmapped, 3 proposed, 8 approved"
- **"Generate OMOP" button** at top — calls `POST resolve-source-codes`, shows spinner during execution, refreshes list on completion
- Table columns: Source Code | Vocabulary | Domain | Rows | Current Concept | Mapping Status | Actions
- Status badges: green=approved, yellow=proposed, red=unmapped/concept-0
- Filter toggles: All / Unmapped only
- Clicking "Map" on an unmapped row: opens `InlineDestinationPicker` inline (reuse from `frontend/src/components/CodeMappings/InlineDestinationPicker.tsx`)
  - On save → existing PATCH `/api/v1/code-mappings/{mapping_id}/` which calls `repoint_clinical_rows` globally
  - Checkbox option: "Also re-resolve this patient's rows now" (calls `resolve-source-codes` for this patient)
  - After save: refresh source codes list

**Modifications to `PatientDetail.tsx`:**
```typescript
// Line 711: Change adminTabs
const adminTabs = canViewOmop ? ["Source Codes", "OMOP"] : [];
// Add sourceCodesIdx before omopIdx
const sourceCodesIdx = canViewOmop ? tabLabels.length - 2 : -1;
const omopIdx = canViewOmop ? tabLabels.length - 1 : -1;
// Add tab description
[sourceCodesIdx]: "Source codes from imported data and their OMOP mapping status."
// Add tab rendering
{sourceCodesIdx >= 0 && activeTab === sourceCodesIdx && personId && (
  <PatientSourceCodesTab personId={personId} />
)}
```

**New TypeScript types** in `frontend/src/types/patient.ts` or new `frontend/src/types/sourceCodes.ts`:
```typescript
interface PatientSourceCode {
  source_value: string;
  omop_table: string;
  concept_id: number;
  concept_name: string | null;
  row_count: number;
  mapping_id: number | null;
  mapping_status: "approved" | "proposed" | "unmapped";
  mapping_target_concept_id: number | null;
  mapping_target_concept_name: string | null;
  source_vocabulary_id: string;
  source_code: string;
}
interface SourceCodesResponse {
  person_id: number;
  source_codes: PatientSourceCode[];
  summary: { total: number; unmapped: number; proposed: number; approved: number };
}
interface ResolveResult {
  resolved: number;
  skipped: number;
  already_resolved: number;
}
```

### Issue 5: Frontend — Bulk Generate OMOP on Patient List

Add "Generate OMOP" button to `PatientList.tsx` bulk actions bar, alongside existing "Delete" button.

**Behavior:**
- Visible when patients are selected AND user is admin/staff
- Click → confirmation dialog: "Re-resolve source codes for N selected patients?"
- On confirm → `POST bulk-resolve-source-codes` with selected person_ids
- Progress indicator polling `GET resolve-runs/{run_id}/` every 2s
- On completion → toast: "Resolved X source codes across N patients"

**Modifications to `PatientList.tsx`:**
- Add "Generate OMOP" button in the bulk actions area (near existing delete button)
- New state for resolve-run tracking (run_id, polling)
- Progress bar or spinner during execution

## Files Modified (by issue)

| Issue | Backend files | Frontend files |
|-------|---------------|----------------|
| 1 | `patient_portal/api/views.py`, `patient_portal/tests.py` | — |
| 2 | `omop_core/mapping/code_resolution.py`, `patient_portal/api/views.py`, `patient_portal/tests.py` | — |
| 3 | `omop_core/models.py`, `omop_core/migrations/`, `patient_portal/api/views.py`, `patient_portal/api/v1_urls.py`, `patient_portal/tests.py` | — |
| 4 | — | `PatientSourceCodesTab.tsx` (new), `PatientDetail.tsx`, `types/sourceCodes.ts` (new) |
| 5 | — | `PatientList.tsx` |

## Key Reusable Code

| What | Where | Used for |
|------|-------|----------|
| `CLINICAL_TABLES` | `omop_core/mapping/code_resolution.py:73` | Iterating all 5 clinical tables |
| `_QUARANTINE_VOCABULARIES` | `code_resolution.py:90` | Identifying HK-\* concepts |
| `approved_mapping_for()` | `code_resolution.py:155` | Looking up approved SCCM mapping |
| `resolve_historical_value()` | `repoint_resolvable_zeros.py:37` | Safe resolution with domain validation |
| `repoint_clinical_rows()` | `code_resolution.py:588` | Global repoint on mapping approval |
| `InlineDestinationPicker` | `frontend/src/components/CodeMappings/InlineDestinationPicker.tsx` | Mapping dialog from patient context |
| `PatientOmopTab` pattern | `frontend/src/components/Patient/PatientOmopTab.tsx` | Reference for tab component structure |
| `SuggestRun` model pattern | `omop_core/models.py` | Pattern for `SourceCodeResolveRun` |
| `derivation_jobs.py` dispatcher | `omop_core/services/derivation_jobs.py` | Celery/inline dispatch for bulk resolve |

## Verification

```bash
# Backend tests (issues 1-3)
/opt/homebrew/opt/postgresql@18/bin/createdb -p 5433 -U postgres promop_test_claude
/opt/homebrew/opt/postgresql@18/bin/psql -p 5433 -U postgres -d promop_test_claude \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" -c "CREATE EXTENSION IF NOT EXISTS vector"

DATABASE_URL="postgresql://postgres@localhost:5433/promop_test_claude" DEBUG=True \
  /Users/adamblum/promop/.venv/bin/python manage.py test \
  patient_portal.tests.PatientSourceCodesTest \
  patient_portal.tests.ResolvePersonSourceCodesTest \
  patient_portal.tests.BulkResolveSourceCodesTest \
  --verbosity=2 --noinput

/opt/homebrew/opt/postgresql@18/bin/dropdb -p 5433 -U postgres promop_test_claude

# Frontend (issue 4-5)
cd frontend && npm run lint && npm run build
```
