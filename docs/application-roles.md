# SODAP role hierarchy and privileges

**SODAP** means **Staff → Org Admin → Doctor → Analyst → Patient**. This is
PROMOP's current application-role reference, including administrative file
uploads and service-token administration. It describes the implemented human
roles; credentials, organization/patient scope, and endpoint rules also constrain
an operation.

## How to read the hierarchy

Staff has platform authority. Org Admin administers particular organizations.
Doctor has clinical write access in assigned organizations or patient groups.
Analyst has clinical read access in those scopes. Patient has access to their
own linked record.

The order expresses administrative authority, not automatic inheritance of every
permission. In particular, a Patient can edit their own supported clinical fields,
while an Analyst cannot edit clinical records through an analyst grant. A Doctor
in organization A has no authority over a patient in organization B merely because
Doctor appears above Patient.

Roles can coexist and combine within their respective scopes. An analyst who
also has a patient link can edit their own record through that link. A doctor
who administers another organization has Doctor privileges in the first scope and
Org Admin privileges in the second. Being an Org Admin never makes someone Staff.

For organization invitation upgrades, `ROLE_RANK` orders `org_admin > doctor >
analyst > patient` and prevents an invitation from downgrading an existing higher
grant. Staff is stored separately as `Identity.is_staff`; that rank is not the
general API authorization algorithm.

## Privilege matrix

These columns describe the named role alone. **In scope** means a valid grant or
patient link authorizes access to the target. Additional roles or verified
representation can add another access path. “Yes” remains subject to the
operation's validation, authentication, and scope requirements.

| Privilege | Staff | Org Admin | Doctor | Analyst | Patient |
|---|---|---|---|---|---|
| Read patient clinical records | Across organizations | In administered orgs / assigned patient groups | In assigned orgs / patient groups | In assigned orgs / patient groups | Own linked record |
| Edit supported clinical fields and use permitted clinical write endpoints | Across organizations | In scope | In scope | No clinical writes through this role | Own linked record |
| Export a permitted patient's FHIR record | Across organizations | In scope | In scope | In scope, with read authorization | Own linked record |
| Use **Upload → FHIR or CSV**, including creating patients by import | Yes | Yes, into an active administered org | No | No | No |
| View the administrative **OMOP** tab | Yes | Accessible patients, with admin authority | No | No | No |
| Use administrative patient deletion (`admin-delete`) | Yes | Only if all owning orgs are administered | No | No | No; separate self-service deletion exists |
| Edit organization settings, including patient self-signup policy | Any org | Administered orgs | No | No | No |
| Invite org users; list/revoke org invitations and access grants; manage org trusts | Any org | Administered orgs | No | No | No |
| Create/delete organizations or change their active status | Yes | No | No | No | No |
| View and propose field/code mappings; work with unapproved proposals | Yes | Yes, subject to curation eligibility below | Yes | Yes | No |
| Approve mappings or delete approved mappings | Yes | With org-admin authority | No | No | No |
| Maintain therapy reference definitions and links | Yes | With org-admin authority | No | No | No |
| Manage service applications and issue/revoke their tokens | Yes | No | No | No | No |
| Read the audit-event API | All events | Own actor events | Own actor events | Own actor events | Own actor events |

### Staff

Staff is the platform operator role, stored as `Identity.is_staff=True`. No
`GroupAccess` row is needed for cross-organization patient access or organization
administration. Staff can create, activate/deactivate, and delete organizations;
perform administrative imports; approve mappings; and administer service
applications and their tokens.

Staff does not mean an unconditional API bypass. OAuth scopes, allowed HTTP
methods, CSRF where enforced, field writability, required confirmations, and
endpoint validation still apply. Token administration specifically requires an
authenticated human staff user; a service credential cannot administer tokens.

### Org Admin

An Org Admin administers an organization through a direct organization grant or
one of the trust paths below. They can manage that organization's settings,
invitations, access grants, and trusts; read and edit its patients; and import
FHIR/CSV patient data into it. A group-scoped `org_admin` grant gives patient
access through that group; it does not by itself confer administration of the
group's entire organization.

Org Admin cannot create/delete organizations, toggle organization activation, or
administer service application tokens. The org access-edit endpoint can switch
Doctor/Analyst grants and change their premium flag, but cannot edit Org Admin
or Patient grants through that PATCH operation. Invitations and revocation are
separate operations; there is no grant-management operation here that promotes
someone to Staff.

Administrative deletion requires control of every organization holding the
patient's record. An unassigned patient is staff-only for that operation. Uploads
also do not move existing patients between organizations. See
[patient file uploads](patient-file-upload.md).

### Doctor

Doctor is a clinical professional role. An active organization grant covers
patients owned by that organization; an active group grant covers patients in
that group. Doctors can read and update authorized clinical records through the
supported clinical endpoints, export readable records, and contribute field/code
mapping proposals.

Doctor alone does not grant organization administration, the bulk FHIR/CSV Upload
flow, mapping approval, or token administration. Patient-facing sync or clinical
editing is distinct from administrative file upload. An organization may grant a
doctor additional Org Admin authority explicitly or through a trust.

### Analyst

Analyst has read-only clinical access to patients in the assigned organization
or groups, including authorized exports. “Read-only” describes clinical records:
analysts can still participate in field/code mapping curation, including proposals,
without gaining mapping-approval authority. Their account/profile operations are
also separate from their clinical role.

An Analyst cannot use administrative Upload, edit another patient's clinical
record through the analyst grant, or manage organizations/tokens. An optional
analytics redirect is stored on the grant; it changes the post-login destination,
not authorization. Analyst replaced the former Navigator role.

### Patient

A `PatientUser` link connects the authenticated identity to its own `Person`.
Patients can read/export their own health record and edit supported fields or use
permitted self-service clinical endpoints. Their own account has a separate
self-service deletion flow. Read-only/computed fields remain read-only regardless
of patient ownership.

A `GroupAccess(role='patient')` membership identifies the patient's organization;
it does not give access to other patients, professional curation, organization
administration, or administrative Upload. Patients can join by invitation or
through an organization's enabled self-registration flow. A verified personal
representative relationship can add access to another specific patient's record;
a patient membership alone cannot do so.

## Scope and endpoint rules

`can_access_patient()` accepts Staff, the linked patient themselves, verified
personal representation, active professional grants, or scoped organization-admin
authority. `can_write_patient()` uses the same access paths but excludes Analyst
from professional write roles. Grants with an `expires_at` in the past no longer
supply access. Public aggregate-data visibility is a separate organization policy;
it does not create a SODAP role or patient-write authority.

Endpoint permissions remain a separate gate. For example,
`ScopedTokenPermission` permits session/partner-authenticated non-staff users to
read and PATCH, while specific clinical CRUD/sync endpoints deliberately use
permissions that allow additional writes. OAuth read/write scopes are enforced
where the endpoint uses scoped-token permissions. A role or a valid token alone
is not a universal grant to every endpoint.

Mapping curation operates on shared reference data, not an organization's private
patient records. Its current eligibility checks are deliberately worth separating:

- Field/code curation entry points use `_can_manage_field_mappings()`: Staff or
  an active professional `GroupAccess` grant (`org_admin`, `doctor`, or `analyst`).
- Approval checks use `_can_approve_mappings()`: Staff or effective org-admin
  authority, including qualifying trusts. A trust alone is not a professional
  `GroupAccess` row, so a domain-trust-only admin may pass the approval helper but
  still fail the separate field/code curation entry gate.
- Therapy reference mutations have their own organization-admin check. Field/code
  proposal privileges do not authorize therapy reference administration.

The patient-list toolbar currently exposes Mappings and Upload to Staff/Org Admin.
Doctor/Analyst mapping routes are available through `/mappings`, `/field-mappings`,
and `/code-mappings`; a hidden toolbar button is not the API permission model.

## Organization-admin trusts

Both trust types intentionally grant **org_admin** authority in the
**granting organization**:

- If Hospital A trusts Clinic B, an active professional organization-level
  grant in B confers org-admin access to A. The inherited role expires when
  its source grant expires.
- If Hospital A trusts an email domain, matching users receive org-admin
  access to A under the existing domain-trust rules.

Trusts do not confer staff or superuser status. Inheritance is one hop; it does
not recursively expand through a chain of trusts. Existing patient-only
organization membership exclusions remain in place for both trust types.
Group-only membership does not trigger organization-to-organization admin
inheritance. Removing the trust or its qualifying grant removes the inherited
role.

`get_admin_access_paths()` supplies the grant sources both to authorization
(`get_admin_orgs()`) and to role reporting. This keeps the displayed inherited
scope aligned with the authority used by organization endpoints.

## Profile API and display

`GET /api/user/` adds:

- `effective_roles`: every active role and its scope, source and expiration.
  Sources are `staff_flag`, `patient_link`, `org_grant`, `group_grant`,
  `organization_trust`, and `domain_trust`. A trust source identifies its
  source organization or email domain. Multiple sources/roles in one
  organization remain visible.
- `patient_delegations`: verified representative relationships, each naming
  the patient record and relationship. These are patient-specific delegated
  access, not a sixth organization role.

The profile displays these separately from pending invitations. The legacy
`org_accesses` list preserves organization metadata, reports each active grant,
includes inherited admin grants, and gives pending invitations `role: null`
with a separate `pending_role`. A pending invitation must not enable a
professional UI route. `access_via: explicit_grant` means a stored grant; it
must not be described as an invitation without evidence of its origin.

The existing `is_patient` and `person_id` properties remain routing hints for
patient-only mode. They are not the exhaustive role inventory: a professional
can have an active patient link even when `is_patient` is false. Clients that
need the complete picture must use `effective_roles`.

## Operational and delegated access

Django's `is_superuser` is retained for operational accounts and Django admin
permission checks. It is not an application role or mode. The supported
`create_superuser()` path requires `is_staff=True`, so its application role is
Staff. Its existing emergency-access behavior is retained.

Verified personal representation, time-limited emergency audit access,
service-token/OAuth scope grants, and premium feature entitlements retain their
separate purposes. A break-glass grant can extend audit-event visibility for its
specified patient; it is not a general clinical read/write grant in
`can_access_patient()` or `can_write_patient()`. None adds an organization role
to the selector.

Service credentials identify applications and authorize only their configured
scopes and applicable tenant access; they are not Staff or an extra SODAP role.
Caller-supplied `actor_iss`/`actor_sub` do not establish a human identity for a
machine request. See [service-token migration](service-token-migration.md).

## Implementation references

Use these entry points when updating this privilege reference:

| Concern | Source |
|---|---|
| Patient read/write authorization and grant scope | [authorization.py](../omop_core/authorization.py) |
| Effective org administration, trusts, professional eligibility | [access.py](../omop_core/services/access.py) |
| Organization grants and stored role choices | [models.py](../omop_core/models.py) (`GroupAccess`) |
| Endpoint permissions and OAuth/service scopes | [permissions.py](../patient_portal/api/permissions.py) |
| Upload role gate and organization selection | [bulk_upload.py](../patient_portal/api/bulk_upload.py) |
| Organization lifecycle, invitations, role rank, grants and trusts | [org_views.py](../patient_portal/api/org_views.py) |
| Patient exports/deletion, clinical APIs and mapping privileges | [views.py](../patient_portal/api/views.py) |
| Audit visibility and verified proxy-grant reporting | [audit_views.py](../patient_portal/api/audit_views.py), [representatives.py](../patient_portal/api/representatives.py) |
| Staff-only service-token management | [service_applications.py](../patient_portal/api/service_applications.py) |
| Effective-role and legacy profile fields | [serializers.py](../patient_portal/api/serializers.py) (`UserSerializer`) |
| Page access and toolbar visibility | [App.tsx](../frontend/src/App.tsx), [PatientList.tsx](../frontend/src/components/Patient/PatientList.tsx) |

Related guides: [patient access and signup](patient-role-org-access-architecture.md),
[identity architecture](identity-architecture.md),
[patient file uploads](patient-file-upload.md), and
[service application administration](service-application-admin.md).
