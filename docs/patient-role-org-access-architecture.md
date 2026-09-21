# Patient Role and Org-Scoped Access Architecture

For the complete Staff, Org Admin, Doctor, Analyst, and Patient hierarchy, see
[SODAP role hierarchy and privileges](application-roles.md).


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

The role a self-signup receives depends on why the org was offered to that visitor:

| Org is reachable because | `GroupAccess` role |
|---|---|
| `allows_patient_signup` (public demo org) | `analyst` |
| it trusts the email domain (`OrgTrust.trusted_domain`) | `patient`, promoted to `analyst` when the address is verified |
| a pending `OrgInvitation` only | `patient` |

A public org's role comes from that org's policy. The other two depend on who the
visitor says they are, and signup does not know that yet — see below.

### A typed address is a claim

Self-signup takes an email and a password and signs the user in. Until the user
follows the link mailed to that address, `Identity.email_verified_at` is null and
`Identity.has_verified_email` is false. **Anything that grants access because of an
email address must read `has_verified_email` / `verified_email_domain`, never
`email`:**

- **Domain trusts** (`omop_core/services/access.py`) apply to a verified domain only.
- **Matching a person by email** (`resolve_or_create_person`) requires a verified address.
- **Pending invitations** appear in account responses only after verification.
- **Signup never claims an existing account.** Passwordless invitees claim their
  account through the emailed invitation. Invitations addressed to an existing
  unverified login withhold the role until acceptance; acceptance requires a new
  password so the mailbox owner replaces any unproved credentials and sessions.

Proof comes from a verification link (`POST /api/v1/auth/verify-email/`), an
emailed invitation or password reset, or a matching verified identity-provider
claim. Operator-created local staff accounts count as verified. Migration
`patient_portal.0020` backfills local staff and accepted invitations, restricts
legacy private-domain signup grants, and removes unconfirmed invitation grants
from unverified password accounts. Other local accounts confirm through the
banner's resend action or password reset. Existing federated accounts establish
verification on their next login with a verified claim.

Only signup grants marked `pending_email_verification` are promoted; ordinary
patient memberships retain their role.

The link is a signed value bound to the account *and* the address, valid 3 days;
nothing is stored. A mail failure never fails a signup.

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
