# PHR-S FM Architecture

This document records the delivered PROMOP patient-facing PHR architecture and its
mapping to the HL7 PHR System Functional Model R2. It replaces the completed Phase 1
and Phase 2 implementation plans.

## Scope

PROMOP implements an oncology-focused PHR subset on top of OMOP CDM and FHIR R4:

- first-class patient account holder identity
- patient self-scoping across patient record and OMOP APIs
- org-scoped patient account provisioning
- own-record viewing and FHIR export
- consent toggles
- patient-originated surveys
- bidirectional patient/provider messaging
- account-holder clinical lists for advance directives, immunizations, and allergies
- reviewable audit events

Full PHR-S FM conformance remains an open program. The current implementation is a
pragmatic subset, with status tracked in `phrs-fm-traceability.md` and
`phrs-fm-conformance-claim.md`.

## Account Holder Identity

The canonical patient role test is the `Identity -> PatientUser -> Person` link. A
patient identity may also have `GroupAccess(role='patient')` rows that tie the patient
to an organization without granting provider-level visibility. Provider roles remain
`org_admin`, `doctor`, and `analyst`.

`patient_portal/services.py::patient_person_for()` resolves whether an authenticated
identity is acting as a patient account holder. Staff, superusers, and identities with
non-patient org grants are not treated as patient-only actors.

`/api/v1/user/` exposes patient-aware fields, including `is_patient`, `person_id`, and
organization access data used by the React SPA.

## Access Control

Patient access is enforced in two layers:

- queryset scoping limits patient-mode list responses to the linked `person_id`
- `PatientSelfScopePermission` performs object-level checks on OMOP and patient-record
  endpoints

The permission is applied across `PatientRecordViewSet` and OMOP-backed clinical
viewsets such as conditions, measurements, observations, procedures, episodes, surveys,
documents, immunizations, allergies, and messages. Service tokens, staff, superusers,
and provider identities continue through the provider/org authorization path.

`can_access_patient()` and `can_write_patient()` remain the backend predicates for
provider and service-token authorization. `GroupAccess(role='patient')` never grants
org-wide patient visibility.

## Patient Portal UI

The React app has role-gated routing. Patient identities land on `PatientHome`; provider
identities land on the existing provider console. Org-scoped routes support patient login,
signup, invite acceptance, and landing on the patient's own record:

```text
/org/:slug/login
/org/:slug/signup
/org/:slug/accept-invite
/org/:slug/
```

`PatientHome` reuses the patient detail tabs for own-record viewing and patient-enabled
actions. The UI does not expose provider patient lists to patient identities.

The section API at `/api/v1/phr/diagnoses/` returns `spread` on the primary
blood cancer card from `PatientRecord.custom_fields.hematologic_spread` (D05,
[#1859](https://github.com/healthkey-ai/promop/issues/1859)). This is the latest
valid Observation projected through the approved custom field mapping. The
mapping deliberately uses concept 0 and the source key `phr:hematologic-spread`
for unmapped narrative text; it does not assign a solid-tumour metastasis concept.
Supported blood cancer slugs are myeloma (including `multiple-myeloma`),
follicular lymphoma, chronic lymphocytic leukemia and mantle cell lymphoma.
Empty values are omitted, and solid tumours continue to use their metastasis
fields. The flat custom field has no per-disease association, so secondary
cancer cards do not inherit it. Shared diagnoses use the same presentation.
HealthTree's PHR app owns card rendering.

For D03 ([#1857](https://github.com/healthkey-ai/promop/issues/1857)), diagnosis
cards also return `note` and `care_team` when recorded against that exact
ConditionOccurrence. The primary card exposes `condition_id` for its latest
condition row; other cards already identify their condition in `id`. A primary
card without a condition row cannot carry per-condition context. Grouped cancer
cards use their latest condition row; other conditions use the displayed row.
Context from a previous condition event is never implicitly copied to another.

`GET/PATCH /api/v1/conditions/{condition_id}/context/` reads or edits these two
strings under the existing condition permissions and patient write authorization.
PATCH preserves omitted fields; blank/null explicitly clears a field. The
approved field mappings determine the concepts and storage tables; migration
[0309](../omop_core/migrations/0309_condition_context_mappings.py) deploys their
domain-checked assignments. Missing vocabulary fails the write with HTTP 400
instead of storing an unmapped or wrong-domain fact. Curator-approved nonzero
assignments survive the migration.

Facts use the CDM condition field concept alongside their event ID, preventing
ID collisions with other OMOP tables. Edits append snapshots under stable source
keys, with overflow text in owned Note rows. Readers batch-load patient-scoped
facts, exclude entered-in-error rows, and respect the latest blank value. Legacy
concept-0 observations under those keys remain readable. Narrative measurements
are excluded from PHR lab results and lab activity counts. Shared diagnosis
cards use the same reader and the existing diagnosis selection controls.
Provenance can be supplied using the standard `X-Provenance-Source` and
`X-Provenance-User-Id` headers. HealthTree's separate app still owns rendering
and editing these fields on its diagnosis cards.

For D04, stage, subtype and marker assertions are projected per cancer using
explicit OMOP condition-event relationships. The primary PatientRecord remains
a compatibility projection, with safeguards against secondary-cancer writes and
clears. See [per-cancer diagnosis details](cancer_details_architecture.md) for the
v1 authoring, source and legacy-data contracts.

## Provisioning

PROMOP supports three patient account paths:

- staff patient invite and accept
- trusted app-driven signup at `/api/v1/patients/signup/`
- org-scoped signup at `/api/v1/orgs/{slug}/patient-signup/` when
  `Organization.allows_patient_signup` is enabled

Patient invitations through `OrgInvitation(role='patient')` may link to an existing
`Person` or create a new `Person`, `PatientRecord`, and `PatientUser` on acceptance.
The acceptance path also creates `GroupAccess(role='patient')` for the org.

Legacy patient invitation routes remain available for backwards compatibility.

## Own-Record FHIR Export

FHIR export is implemented as a server-side OMOP-to-FHIR R4 projection:

- service: `omop_core/services/fhir_export.py::build_fhir_bundle()`
- API: `GET /api/v1/patient-records/{person_id}/export-fhir/`
- command: `manage.py export_fhir_bundle`
- frontend: download action from the patient record UI

The export endpoint returns an `application/fhir+json` Bundle and is guarded by the same
object authorization as patient record detail. Patients can export only their linked
record; providers and service callers must pass the normal patient access checks.

## Consent

`PatientConsentViewSet` is mounted at `/api/v1/consents/`. It auto-creates the supported
consent rows on first list, self-scopes to the authenticated patient, and exposes PATCH
toggle behavior. Consent timestamps update when the consent state changes.

## Patient-Originated Data

`PatientCrudPermission` allows session-authenticated patients to create and update the
patient-originated resources intended for them, while preserving patient self-scope.
Surveys are available in patient mode with list, start, continue, autosave, completion,
and read-only completed states.

## Messaging

`PatientMessageViewSet` is mounted at `/api/v1/messages/` and supports threaded messages,
sender tracking, read state, pagination, self-scoping, cross-patient reply protection,
and `mark-read`.

## Clinical Lists

Patient-mode clinical lists use existing OMOP facts with explicit source tags:

- advance directives use `PatientDocument(doc_type='ADVANCE_DIRECTIVE')`
- immunizations are exposed from vaccine-tagged `DrugExposure` rows at
  `/api/v1/immunizations/`
- allergies are exposed from allergy-tagged `Observation` rows at `/api/v1/allergies/`

## Audit

`AuditLogMiddleware` audits API and OAuth traffic by writing structured stdout JSON and
an immutable `AuditEvent` row. Events are classified as record view/create/update/delete,
auth, consent, break-glass, and related security events. Audit persistence is guarded so
audit failure does not block the clinical response.

`GET /api/v1/audit-events/` is read-only. Staff and service callers can review all rows;
patients see only events associated with their own record.

## Open Conformance Work

The current architecture does not claim full PHR-S FM R2 compliance. Remaining work
includes standards-based FHIR `AuditEvent`/`Provenance` alignment, broader record
lifecycle coverage, security completion, care plans, education, additional encounter
flows, interoperability governance, and supportive/admin functions.
