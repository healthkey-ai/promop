# Patient file uploads

From the patient list, select **Upload** (to the right of **Mappings**), then
**FHIR** or **CSV**. Staff and organization administrators can open either
upload page. Direct links `/upload-fhir` and `/upload-csv` remain available.

Select the organization that will own new patients. Organization administrators
can select only organizations they administer; a single organization is selected
automatically. Staff may leave the organization unassigned. An upload cannot
move an existing patient into another organization or update a patient outside
the caller's authorized scope.

FHIR uploads accept a JSON Bundle with a unique ID for each Patient resource.
Clinical resources must reference a Patient in the same bundle, using its ID,
`Patient/{id}`, or its entry's `fullUrl`. Patients may appear anywhere in the
bundle. Empty bundles, duplicate patient references, and orphaned clinical
resources are rejected before any patient is written. Per-patient processing
failures are reported alongside created and updated counts; successful patients
remain imported. Patient identity matching still uses the existing name and
birth-date matching behavior.

CSV supports the columns documented on its upload page, including demographics
and dated diagnoses. Both upload pages report newly created and updated patients.
Clinical facts flow through OMOP and the existing PatientRecord derivation.

The existing machine upload endpoints retain their credential scope checks and
organization restrictions. Human browser uploads require staff or organization
admin authority; OAuth user tokens additionally require a write scope. Session
uploads enforce CSRF. Import provenance identifies the authenticated human or
service credential, ignoring caller-supplied user attribution.
