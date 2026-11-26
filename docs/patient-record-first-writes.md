# PatientRecord-First Write Architecture

This is the current architecture for interactive edits. UI edits save to PatientRecord;
OMOP-derived values are not read-only merely because they were imported.
Explicit computations such as BMI remain read-only for authorized editors.

## Problem

The original write architecture had two frontend paths:

1. **OMOP-mapped fields** (hemoglobin, ANC, etc.) wrote to OMOP clinical endpoints
   (`POST /v1/measurements/`, `POST /v1/observations/`), then relied on `post_save`
   signals to re-derive `PatientRecord`.

2. **Unmapped fields** (planned_therapies, etc.) wrote directly to `PatientRecord` via PATCH.

This created several problems:

- **Split brain**: the frontend needed to classify every field and route it to the
  right endpoint, duplicating backend logic.
- **User edits lost on derivation**: when derivation re-ran (triggered by any OMOP
  write), it overwrote PatientRecord from OMOP tables — wiping any value that wasn't
  yet backed by an OMOP fact.
- **Mapping-approval disconnect**: approving a `FieldConceptMapping` didn't
  retroactively project existing user edits into OMOP, leaving a gap.
- **Fragile save path**: the frontend had to fetch the writable-field descriptor,
  split edits by target, write OMOP facts individually (with supersede logic), then
  PATCH only the remaining fields — and an error in any step could leave the save
  half-done.

## Solution

**All UI edits write to PatientRecord first.** The backend handles OMOP projection
as a side-effect for fields with approved mappings.

### Write flow

```
User edits a field
    |
    v
Frontend: PATCH /api/patient-info/{person_id}/
    |  (all writable fields in one request, including Person profile fields)
    v
Backend: PatientRecordSerializer.save()
    |  (value lands on PatientRecord immediately)
    |
    +---> user_edited_fields tracking (for derivation preservation)
    |
    +---> _project_mapped_fields()
    |       |
    |       +---> For each changed, validated field with a descriptor projection:
    |       |       project_single_value() upserts the OMOP fact
    |       |
    |       +---> acknowledge successful scalar projections
    |
    +---> recompute aliases and calculated fields from PatientRecord
    |
    v
Response to frontend
```

### Profile fields

Since PR #1163, profile edits use the same PatientRecord PATCH as clinical
edits. The backend saves profile values, then `_project_profile_fields()` updates
Person (demographics, contact information, and birth fields) or its Location row
(address and coordinates), without calling `refresh_patient_record()`.
Patient name uses the same PATCH and updates Person's name columns; its response
field is not a stored PatientRecord column. Clinician validation fields retain
their staff access checks.

### Confirming a proposal

A value the record shows as Provisional (from a mapping not yet approved) is not on
`PatientRecord`. A patient approves one by entering it, which is this PATCH. A professional
who may write the patient approves it with
`POST /api/v1/patient-records/{person_id}/confirm-provisional/`
(`omop_core/services/provisional_confirm.py`), which is the same write with the value taken from
the server's current proposal instead of the client: it locks the record, reads the overlay, and
calls `apply_patient_record_patch` once with every confirmed field, so the value lands on
`PatientRecord` first, is tracked in `user_edited_fields` until OMOP holds it, projects through the
field's descriptor, and drops its mark, exactly as an equivalent PATCH does (a revision row names the
caller). The projected fact is dated when the reading was taken (`as_of`), not today as a typed edit's is.
Only fields an OMOP projection (or a Person/Location column) acknowledges are confirmable: any other would stay
pinned in `user_edited_fields`. It writes no clinical row of its own beyond that projection, approves no
mapping, and never sets `validated`. See the provisional data plan, "Staff confirm provisional values".

### Created fields

A field added through Field Concept Mapping (`CustomPatientField`) has no
column: its value is a key of `PatientRecord.custom_fields`. Its edits follow
the same path (#2669, `omop_core/services/custom_field_values.py`):

- **The PATCH carries `custom_fields: {<name>: <value>}`**, partial by key:
  a key not named is untouched, and one whose value is what the record holds
  is not an edit. The editors send only the keys the user changed
  (`frontend/src/utils/addedFieldEdits.ts`), never the object they read,
  which also holds computed results and other people's values as they were.
  A key that is not an approved, non-computed created field is ignored, as a
  read-only column is. Values are coerced by the field's type (text, number,
  ISO date, boolean); `null` or `""` clears; a value of the wrong type is a
  400 that names the field and saves nothing.
- **PatientRecord first.** The value is written to `custom_fields` and marked
  pending in `user_edited_fields` as `custom_fields.<name>`; no column name
  has a dot, so the two never collide. A `RecordRevision` is written under the
  same name.
- **Forward into OMOP** with `project_single_value`, from the field's own
  mapping and under its source key (`custom:<name>`), typed by the author as
  any other edit. A Measurement or Observation projection acknowledges the
  edit. An occurrence table records a boolean yes and nothing else, since an
  occurrence cannot hold a number, a date, text or a no; a table with no
  writer, a source key longer than the table's source column and a failed
  write all leave the value pending on the record.
- **Derivation keeps it.** `refresh_patient_record` rebuilds `custom_fields`
  from OMOP and puts a pending value back until derivation produces the same
  answer. An entered row is read back as entered (a boolean's 0 or 1, a
  date's own value); an imported row answers a boolean by existing and a date
  by its own date. The latest fact answers the field, so a newer import
  replaces an entered value and on one day the entered value wins. A value
  cleared in the editor stays cleared against imported facts of that day or
  before (`cleared_days`); a later import, or a value entered after the
  clear by anyone, becomes current.
- **The descriptor lists them.** `GET /api/v1/patient-records/writable-fields/`
  adds an entry per created field (`custom: true`, `container:
  "custom_fields"`), writable when approved and not computed, read-only for a
  caller without write access, and scoped by `?disease=` like a column. The
  entries are added at the endpoint, not in `build_writable_field_descriptor`,
  which the save path calls on every PATCH.

- **Provisional values (#2857).** With provisional data on, a proposal-based row also
  answers a created field the record leaves empty, marked
  `provisional_fields["custom_fields.<name>"]`; the PATCH of that key is an approval
  and drops the mark (`mirror_user_edit`). See [the plan](provisional_data_plan.md#created-fields-as-built-2857).

`recompute_formula_field`, which runs when a formula changes, writes only
the computed keys of `custom_fields`, under the record's lock, so it cannot
put an entered value back to what it was.

Not covered: whether the field belongs to the record's disease is not
enforced (#2333), and a pending created-field edit is not re-projected when
its mapping is changed and re-approved.

## Write Descriptor Changes

The `build_writable_field_descriptor()` function now returns `KIND_DIRECT` with
`target: 'patient_record'` for all clinical fields. Fields with approved OMOP
mappings carry a `projection` key:

```python
# Field with an approved mapping (e.g., hemoglobin via LOINC 718-7):
{
    'kind': 'direct',
    'writable': True,
    'target': 'patient_record',
    'value_kind': 'number',
    'unit': 'g/dL',
    'projection': {
        'omop_table': 'measurement',
        'concept_id': 3000963,
        'code': '718-7',
        'vocabulary': 'LOINC',
        'display': 'Hemoglobin',
        'unit': 'g/dL',
        'unit_concept_id': 8713,
        'type_concept_id': 32865,
        'source_value': '718-7',
    },
}

# Field without a mapping (e.g., planned_therapies):
{
    'kind': 'direct',
    'writable': True,
    'target': 'patient_record',
    'value_kind': 'string',
    'reason': 'Written directly to PatientRecord. No OMOP mapping yet.',
}
```

### KIND_EDITABLE is retired

The `KIND_EDITABLE` constant still exists for backward compatibility but is no
longer emitted by the descriptor builder. All writable clinical fields are
`KIND_DIRECT`.

### Vocabulary not loaded

When the vocabulary doesn't carry a concept (e.g., LOINC not loaded), the field is
still `KIND_DIRECT` and writable — the value lands on PatientRecord and is preserved
across derivation. No `projection` key is present, so no OMOP fact is created.

## OMOP Projection

### At PATCH time (`project_single_value`)

Both the provider PATCH and `/patient-info/me/` use the same save path.
Changed, validated fields use the writable descriptor, including built-in
LOINC/SNOMED recipes and approved curated mappings, to call
`project_single_value()`:

- Matches `(person_id, concept_id, source_value, current local date)`, excluding
  erroneous rows. Repeated edits on the same day update that day's row, including
  a matching imported row. Its original provenance is retained.
- Creates a new row dated today when there is no matching row today. Earlier
  dates remain unchanged as history.
- Carries the descriptor's value kind, units, and unit concept into the fact.
- Types the fact by its author. When the patient (`/patient-info/me/`) or a
  verified representative saves, the fact is Patient self-report (32865), not
  the recipe's type, and same-day matching considers only facts of that type:
  a patient's answer is its own fact beside a clinical one, never an update
  that takes over the clinical fact's type and provenance. The patient's own
  writes also get a `PATIENT_SELF` `ProvenanceRecord`, as PHR entries do.
  Other editors keep the recipe's type (curated recipes default to EHR,
  32817) and never update a patient's self-report fact, unless the recipe is
  itself typed self-report. Without concept 32865 loaded, the recipe's type
  stands (#1959).
- Serializes same-day writes with database locks. Projection uses a savepoint,
  so a failed projection leaves the PatientRecord edit pending.
- A cleared Measurement or Observation stores null answer columns with
  `value_source_value = "PatientRecord:cleared"`. Derivation excludes that row
  and preceding rows with the same concept/source key, without changing stored
  history. A subsequent day's result becomes current; correcting today's clear
  reuses today's row and removes the marker.
- Occurrence tables cannot express null or negative answers. Such edits remain
  pending on PatientRecord until an appropriate answer mapping is available.
- Sets `_skip_patient_record_refresh = True` on the OMOP instance to avoid
  recursive derivation.
- Direct UI saves never call `refresh_patient_record()` or load the patient's
  OMOP history, even when projection creates or updates a fact. PatientRecord
  already contains the entered value. Aliases, BMI, receptor calculations, and
  approved formulas are recomputed from its current fields instead.
- Successful scalar Measurement/Observation projections remove the field's pending
  edit marker, including when today's fact already has the same value. Failed,
  unmapped, structured, and occurrence-only writes retain their pending protection.
- External OMOP imports, corrections, deletions, and explicit refresh requests
  still use the full OMOP-to-PatientRecord refresh. Approved scalar mappings
  are read during that refresh, so fields without a built-in extractor also
  retain their projected values and can receive newer imported results.
- Direct edits do not advance `derived_at` or `derivation_version`; those
  identify the last full OMOP refresh.

### At mapping approval (`project_field_to_omop`)

When a curator approves a `FieldConceptMapping`, the `post_save` signal triggers
`project_field_to_omop()`, which backfills all PatientRecords that have a
pending user-edited value for that field. It filters by `user_edited_fields`,
not by comparing typed columns to an empty string, and uses the same dated
writer and unit recipe as PATCH. Records with no pending edits are untouched.

## Derivation Preservation

`refresh_patient_record()` snapshots `user_edited_fields` before derivation and
restores pending values, including nulls, until derivation represents the saved
value. Matching fields drop from `user_edited_fields`; stale facts cannot erase
a pending edit after a projection failure. Dependent calculations run after
pending values are restored.

## Frontend Simplification

### Before (split brain)

```typescript
// Clinical edits → OMOP endpoints
await writeFieldValues(personId, clinicalEdits);
// Direct edits → PatientRecord PATCH
await api.patch(`/patient-info/${personId}/`, directFields);
```

### After (single path)

```typescript
// Clinical and profile edits → one PatientRecord PATCH
await api.patch(`/patient-info/${personId}/`, patchFields);
```

The frontend does not choose an OMOP table. Clinical and profile edits share
the PatientRecord PATCH; the backend routes onward writes to Person/Location or
mapped clinical facts. Full OMOP-to-PatientRecord derivation belongs to the
import/ingestion path, not an interactive save.

## Key Files

| File | Role |
|------|------|
| `omop_core/services/write_descriptor.py` | Builds field descriptors with projection metadata |
| `omop_core/services/omop_projection.py` | `project_single_value()` (PATCH-time) and `project_field_to_omop()` (mapping approval) |
| `omop_core/services/custom_field_values.py` | Created fields: coercion, pending tracking, projection and read-back |
| `patient_portal/api/views.py` | `partial_update()` calls `_project_mapped_fields()` after save |
| `omop_core/services/patient_record_service.py` | Derivation snapshots/restores `user_edited_fields` |
| `omop_core/signals.py` | `post_save` on `FieldConceptMapping` triggers projection |
| `frontend/src/components/Patient/PatientDetail.tsx` | Provider editor doSave |
| `frontend/src/federation/PatientInfo.tsx` | Federation view doSave |
| `frontend/src/hooks/useWritableFields.ts` | FieldDescriptor type with projection |

## Invariants

1. **Every user edit is captured immediately** — the PatientRecord PATCH always
   lands, regardless of mapping state. No edit is lost because a field lacks a
   mapping.

2. **Projection is best-effort** — a projection failure does not roll back the
   PatientRecord write. The value is safely stored and projection can be retried.

3. **Derivation cannot overwrite user edits** — `user_edited_fields` tracking
   ensures user values persist until derivation represents the saved value.

4. **Mapping approval triggers backfill** — existing user edits are projected
   when a mapping is approved, without any manual step.

5. **The frontend has one write path** — no more split between OMOP and
   PatientRecord. The descriptor still carries projection metadata for the date
   picker UI, but routing is server-side.
