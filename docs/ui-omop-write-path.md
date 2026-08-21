# UI → OMOP write path

**Status:** design, not implemented.
**Problem it solves:** no clinical field in the provider patient editor can be saved.

## Where we are

The derive-only migration made every concrete `PatientRecord` clinical column read-only at
the API. That is the intended end state and is enforced by contract test:

```python
# tests/test_patient_record_derive_only_contract.py
assert writable == set()   # no writable concrete PatientRecord data columns
```

The React editor was never migrated. It still `PATCH`es `/api/patient-info/{id}/` with the
record it loaded, so every save is refused. Measured against `dev`:

| Request | Result |
|---|---|
| Whole record, one field changed | `405` |
| Minimal `{"stage": "IV"}` | `405` |

Both are correct server behavior. The client is what is out of date.

Renaming is the one exception and is handled separately: `patient_name` is rendered from
`Person.given_name` / `family_name`, so the compatibility route writes the `Person` row and
lets derivation follow — which the mapping doc already sanctions ("A compatibility endpoint
may update a Person attribute … it writes the source row and rederives").

## Target

An edit in the UI writes a **complete, dated, provenance-bearing OMOP fact**. Derivation
then rebuilds the projection from it. Nothing writes `PatientRecord`.

```
user edits Hemoglobin = 12.5
        │
        ▼
POST /api/v1/measurements/
  { person, measurement_concept: <LOINC 718-7>, measurement_date,
    value_as_number: 12.5, unit_concept: <g/dL>,
    measurement_source_value: "718-7" }
        │  post_save signal
        ▼
refresh_patient_record  →  PatientRecord.hemoglobin_g_dl = 12.5
```

Single-row writes fire `post_save`, so derivation is automatic and the UI does **not** need
`POST /patient-records/{id}/refresh/` — which is just as well, since that endpoint is
admin/service-token only and a treating clinician is neither.

## Coverage is partial, and that governs the whole design

| | Count |
|---|---|
| Read-only mapped `PatientRecord` fields | 319 |
| Fields with a canonical LOINC write mapping (`LAB_FIELD_TO_LOINC`) | 45 |
| **Mapped fields with no defined write target** | **274** |

`docs/omop_to_patientrecord.md` marks many of the remaining 274 as "pending a reviewed
concept set" — they cannot be made writable without vocabulary work, and inventing a
concept per field is exactly what the derive-only migration removed.

So the UI cannot be made wholly editable, now or in one step. **The client must therefore be
driven by a server-supplied descriptor rather than by hardcoded knowledge**, so that a field
becomes editable when its mapping lands, with no frontend change.

## Proposed pieces

### 1. `GET /api/v1/patient-records/writable-fields/`

Read-only, returns one entry per projection field:

```json
{
  "hemoglobin_g_dl": {
    "writable": true,
    "target": "measurement",
    "concept_id": 3000963,
    "code": "718-7",
    "vocabulary": "LOINC",
    "display": "Hemoglobin [Mass/volume] in Blood",
    "value_kind": "number",
    "unit": "g/dL",
    "unit_concept_id": 8713
  },
  "planned_therapies": {
    "writable": false,
    "reason": "No reviewed concept set — see docs/omop_to_patientrecord.md"
  }
}
```

Server-side because `concept_id` is resolved from the vocabulary tables and moves with
vocabulary releases; a TypeScript copy would rot silently. Built from `LAB_FIELD_TO_LOINC`
initially, with room for observation/condition maps as they are defined.

### 2. Client writes the fact

`PatientDetail` stops sending clinical fields to `/patient-info/`. On save it emits one OMOP
write per changed field, using the descriptor, and sends the existing provenance headers
(`X-Provenance-Source`, `X-Provenance-User-Id`). Fields with `writable: false` render
read-only with the reason as a tooltip, rather than silently failing on save.

The rename keeps its current route — it is a `Person` write, not a clinical fact.

### 3. Every clinical edit needs a date

This is the real UX change, not the plumbing. A lab value without an event date is not a
fact, and derivation drops undated rows ("unknown-date facts are not projected"). The
editor currently offers no date input for most fields. Each editable field needs one,
defaulting to today.

### 4. Corrections vs new results

Editing a value that already exists on the same day is ambiguous: a second result, or a fix
to a mistyped one? The bulk upsert key for `Measurement` is
`(source_value, date, datetime, value_as_number)`, so a changed value inserts a new row —
correct for a genuine second result, wrong for a typo.

`Measurement` already carries `is_erroneous` / `erroneous_reason` from the PHR-S FM
entered-in-error work. Proposal: a correction marks the prior row erroneous and inserts the
replacement, leaving an auditable trail; a plain new value on a new date just inserts.
**Open question** — needs a call before implementation.

## Phasing

1. **Descriptor endpoint + tests.** No UI change; safe to land alone.
2. **Labs tab** — the 45 mapped fields, with date input and the read-only treatment for
   everything else. Proves the pattern on the best-mapped group.
3. **Remaining tabs**, as concept sets are reviewed and added to the descriptor.
4. **Retire clinical PATCH** on `/api/patient-info/{id}/` once no caller depends on it.

## Open questions

- Correction semantics (above).
- `measurement_type_concept` for a clinician-entered value — needs a chosen concept.
- Whether an org-scoped clinician should be able to trigger derivation explicitly, or
  whether relying on `post_save` is sufficient (it is, for single-row writes).
- Fields whose UI control is a free-text box but whose OMOP representation needs a coded
  value: these should stay read-only rather than minting local concepts.
