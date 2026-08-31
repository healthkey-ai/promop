# Patient Role and Org-Scoped Access Architecture

PROMOP supports first-class patient identities inside the existing organization access
model. Patients can be invited by an org, self-register where enabled, log in through
org-scoped routes, and land directly on their own patient record.

## Backend Model

`Organization.allows_patient_signup` controls whether public patient self-registration is
enabled for an org.

`GroupAccess(role='patient')` links a patient identity to an org without granting
provider-level record access. Provider roles remain `org_admin`, `doctor`, and `analyst`.

`OrgInvitation(role='patient')` supports patient-specific invites and may include a
`person` reference. If a person is present, accepting the invite links the identity to
that existing record. If no person is present, acceptance creates a new `Person`,
`PatientRecord`, and `PatientUser`.

## Patient Role Resolution

`patient_person_for()` treats an identity as a patient when it has a `PatientUser` and no
active non-patient provider grants. Patient org membership therefore does not turn a
patient into a provider.

`PatientSelfScopePermission` continues to restrict patients to their own `person_id`.
`GroupAccess(role='patient')` never grants visibility into other records in the org.

## API

```text
POST /api/v1/orgs/{slug}/patient-signup/
GET  /api/v1/orgs/{slug}/public/
POST /api/orgs/{slug}/invite/      role=patient
```

Org serializers expose `allows_patient_signup` so admins can enable or disable public
signup. User responses include the org data needed for org-scoped patient routing.

## Frontend Routes

```text
/org/:slug/login
/org/:slug/signup
/org/:slug/accept-invite
/org/:slug/
```

`OrgLogin` and `OrgSignup` fetch public org metadata before rendering. When a patient logs
in through an org route, the app redirects to `/org/:slug/`, which renders `PatientHome`
for the patient's own record.

## Invitation Emails

Patient, doctor, and analyst invitation links use org-scoped accept URLs. After accepting,
patients are sent to the org patient home route; providers return to the provider-facing
application.

## Compatibility

The older patient invitation route remains in place as a fallback while new invites use
the org-scoped flow.
