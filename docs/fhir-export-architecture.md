# FHIR Export Architecture

PROMOP exports a patient's OMOP record as a FHIR R4 Bundle for patient download,
server-side integrations, and operational bulk export.

## Entry Points

```text
GET /api/v1/patient-records/{person_id}/export-fhir/
manage.py export_fhir_bundle --person-id <id> --output <path>
manage.py export_fhir_bundle --org <slug> --output <path>
```

The API endpoint is an action on `PatientRecordViewSet`. The management command calls the
same export service directly so large exports are not constrained by HTTP request limits.

## Authorization

The endpoint uses `ScopedTokenPermission` and `PatientSelfScopePermission`. A patient can
export only their linked `Person`; provider and service callers must satisfy the existing
org and patient-access predicates before exporting a record by URL id.

## Service Boundary

`omop_core/services/fhir_export.py::build_fhir_bundle(person)` is the canonical export
function. It queries the OMOP tables for one `Person` and returns a Python dictionary
representing a FHIR Bundle. HTTP response formatting and file writing live outside the
service.

## Mapping

| OMOP source | FHIR resource |
|---|---|
| `Person` | `Patient` |
| `ConditionOccurrence` | `Condition` |
| `Measurement` | `Observation` |
| `Observation` | `Observation` |
| `DrugExposure` | `MedicationStatement` |
| vaccine-tagged `DrugExposure` | `Immunization` |
| `ProcedureOccurrence` | `Procedure` |
| `VisitOccurrence` | `Encounter` |

Concept coding uses source values and OMOP `Concept` metadata where available. Vocabulary
systems are mapped to standard FHIR coding systems such as LOINC, SNOMED CT, and RxNorm.

## Performance

The service bulk-queries each OMOP table for the requested person and builds the Bundle in
memory. Org-level export processes one patient at a time through the same function.

## Verification

Coverage lives in `patient_portal/tests.py` around the `export-fhir` endpoint and related
authorization cases. The frontend download path is wired through the patient record UI.
