# Adding PatientRecord fields from Patient Info tabs

Issue: #721

## Delivery checklist

- [x] Define the runtime-safe data model: a `CustomPatientField` definition and
  `PatientRecord.custom_fields` JSON payload. A user-created field is therefore
  a persisted PatientRecord field without attempting to mutate the Django
  schema at runtime.
- [ ] Add schema migration, model validation, and an approved-mapping-only
  creation service. `CustomPatientField` owns `field_name`, display label,
  selected tab, value type, and its `FieldConceptMapping`; `custom_fields` is
  the PatientRecord JSON object keyed by field name.
- [ ] Add an admin-only API that requires explicit confirmation that the field
  will be added to PatientRecord, creates the field definition and approved
  OMOP mapping atomically, and rejects duplicate/invalid names.
- [ ] Extend descriptor/mapping APIs so custom fields are visible and retain
  their selected Patient Info tab.
- [ ] Add generic OMOP value derivation for Measurement, Observation,
  ConditionOccurrence, ProcedureOccurrence, DrugExposure, and Person-backed
  custom mappings; persist the latest applicable value in
  `PatientRecord.custom_fields`.
- [ ] Include dynamic field values in Patient Record API responses and make
  them read-only through the existing clinical-write policy.
- [ ] Add an Add field control to every Patient Info tab for mapping admins,
  with explicit PatientRecord confirmation, field name/label/type input, then
  the same vocabulary concept search and OMOP table picker used by the Concept
  Mapping dialog.
- [ ] Render configured custom fields at the bottom of their selected Patient
  Info tab for all users.
- [ ] Add backend derivation/API and frontend workflow/display tests.
- [ ] Run targeted checks, full CI, review the PR, and merge.

## Invariants

- A custom field is visible only after its mapping is approved.
- The mapping and custom-field definition are created in one transaction.
- Values originate from OMOP facts; patient-info editing never writes a custom
  clinical value directly to the PatientRecord projection.
- Field names are stable, lower-snake-case identifiers and cannot collide with
  concrete PatientRecord fields or another custom field.
