# Phase 2 — FHIR Export Sub-plan

Detailed implementation plan for the OMOP → FHIR R4 export capability.

## Strategy

Invert the existing `upload_fhir_bundle` mapping. The upload handler already maps FHIR →
OMOP; export reverses this direction. Use `*_source_value` fields and
`Concept.concept_code` for FHIR coding (LOINC codes for Observations/Measurements, SNOMED
for Conditions, RxNorm for medications).

## OMOP → FHIR Resource Mapping

| OMOP Table | FHIR Resource | Key Fields |
|---|---|---|
| `Person` | `Patient` | `given_name`, `family_name`, `date_of_birth`, gender concept → FHIR `gender` |
| `ConditionOccurrence` | `Condition` | `condition_concept` → SNOMED code, `condition_start_date` → `onsetDateTime` |
| `Measurement` | `Observation` | `measurement_concept` → LOINC code, `value_as_number` / `value_as_concept` → `valueQuantity` / `valueCodeableConcept` |
| `Observation` | `Observation` | `observation_concept` → code, `value_as_number` / `value_as_string` |
| `DrugExposure` | `MedicationStatement` | `drug_concept` → RxNorm code, `drug_exposure_start_date` → `effectivePeriod` |
| `DrugExposure` (immunization) | `Immunization` | Filtered by `drug_type_concept_id` or source value containing "vaccine" |
| `ProcedureOccurrence` | `Procedure` | `procedure_concept` → SNOMED/CPT code, `procedure_date` |
| `VisitOccurrence` | `Encounter` | `visit_start_date` → `period`, `visit_concept` → `class` |

## API Endpoint

```
GET /api/v1/patient-records/{id}/export-fhir/
```

- `@action(detail=True)` on `PatientRecordViewSet`
- Guarded by existing `ScopedTokenPermission` + `PatientSelfScopePermission` — patients
  can only export their own record
- Returns `application/fhir+json` content type
- Response is a FHIR R4 `Bundle` of type `searchset`

## Management Command

```bash
.venv/bin/python manage.py export_fhir_bundle --person-id 123 --output patient_123.json
```

Mirrors the `import_fhir_bundle` pattern: uses Django's `RequestFactory` to call the API
view in-process, avoiding Render's 30-second request timeout for large exports.

Supports `--org <slug>` for bulk export of all patients in an organization.

## Implementation Steps

1. **Export service** (`omop_core/services/fhir_export.py`): pure function
   `build_fhir_bundle(person: Person) -> dict` that queries all OMOP tables for the person
   in one pass and builds the Bundle in memory.

2. **Concept code resolution**: for each OMOP concept FK, look up `Concept.concept_code`
   and `Concept.vocabulary_id` to produce `coding.system` + `coding.code`. Map vocabulary
   IDs: `LOINC` → `http://loinc.org`, `SNOMED` → `http://snomed.info/sct`, `RxNorm` →
   `http://www.nlm.nih.gov/research/umls/rxnorm`.

3. **API action**: thin wrapper calling the service, returning `Response(bundle)`.

4. **Management command**: `export_fhir_bundle` with `--person-id` and `--output` args.

5. **Frontend**: "Download my record (FHIR)" button on `PatientHome`, triggering a
   download of the JSON response.

## Round-trip Testing

Import a FHIR bundle → export → verify core resources are preserved:

```python
def test_import_export_roundtrip(self):
    # Import
    self.client.post('/api/v1/patient-records/upload_fhir/', ...)
    # Export
    resp = self.client.get(f'/api/v1/patient-records/{pr.pk}/export-fhir/')
    bundle = resp.json()
    # Verify Patient, Condition, Observation resources present
    resource_types = {e['resource']['resourceType'] for e in bundle['entry']}
    self.assertIn('Patient', resource_types)
    self.assertIn('Condition', resource_types)
    self.assertIn('Observation', resource_types)
```

## Performance Considerations

- Bulk-query all OMOP tables for the person in one pass (not N+1 per resource)
- Build Bundle in memory — typical patient record is <1MB JSON
- For org-wide export (management command), process patients in batches
- Consider streaming JSON for very large bundles (future optimization)
