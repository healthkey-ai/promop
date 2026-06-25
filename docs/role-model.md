# Role Model & Access Control

## Overview

This document defines how HealthKey services authorize access to patient
data. It builds on the [Identity Architecture](identity-architecture.md),
which covers authentication (who you are). This document covers
authorization (what you can do).

The customer owns all patient data and the database it lives in. HealthKey
services are software the customer deploys to manage that data on the
customer's behalf. Authorization tables live in the same customer-owned
database as the clinical data.

### Two Operating Modes

Authorization works the same way in both modes defined in the identity
architecture:

| Mode | Database | Identity Source | Authorization Tables |
|---|---|---|---|
| **Standalone** | Service-local database, owned by the customer | Local identities (`iss="urn:local"`) | In the service's own database |
| **Integrated** | Customer's shared database, all services connected | Customer's IdP (e.g. Firebase) | In the customer's shared database, queryable by all services |

In integrated mode, the customer provides a single database that all
HealthKey services connect to. Authorization tables (groups, memberships,
access grants) live alongside the clinical data. Any service can run
`can_access_patient()` directly against the shared database — no
cross-service API calls needed for access checks.

Switching between modes is a settings change (same as identity). The
authorization schema is identical in both modes.

---

## Roles

| Role | Can upload/modify PHR | Can create patients | Can manage groups | Can invite professionals | Scope |
|---|---|---|---|---|---|
| **admin** | Yes (on behalf of group patients) | Yes | Yes | Yes | All patients in assigned groups |
| **navigator** | Yes (on behalf of group patients) | Yes | No | No | Patients in assigned groups |
| **doctor** | Yes (on behalf of group patients) | Yes | No | No | Patients in assigned groups |
| **patient** | Yes (own + represented persons) | Yes (represented persons) | No | Yes (grant access to professionals) | Own data + personal representatives |

### Role Semantics

**admin** — organization administrator. Full access to all patients in their
assigned groups. Can create patient groups, assign patients to groups, and
invite other professionals.

**navigator** — patient navigator / care coordinator. Uploads labs, modifies
PHR records on behalf of patients in their assigned groups. Cannot create or
modify groups themselves.

**doctor** — clinician. Same access as navigator: upload/modify PHR on behalf
of group patients. Separated from navigator for audit trail clarity and
potential future permission differentiation.

**patient** — the data owner. Can upload and modify their own PHR. Can join
the system independently or be invited by a professional. Can grant access
to professionals (invite a navigator/doctor to manage their records).

A patient may also act as a **personal representative** for other people:
a minor child, elderly parent, family member, friend, etc. In this case
one Identity manages multiple Person records. See "Personal Representatives"
below.

---

## Personal Representatives

A user joining as a patient may not be managing only their own health records.
Common scenarios:

- Parent managing a minor child's PHR
- Adult child managing an elderly parent's PHR
- Spouse or partner managing records for a family member
- Friend or caregiver helping someone who cannot self-manage

### Model

```
PersonalRepresentative
  representative    — FK → Identity (the person who manages)
  person            — FK → Person (whose PHR is being managed)
  relationship      — free text or enum: parent, child, spouse, guardian, caregiver, other
  granted_at
  granted_by        — FK → Identity (who authorized this: self, the patient, or an admin)

  UNIQUE(representative, person)
```

A personal representative has the same rights as the patient themselves:
upload, modify, view the represented person's PHR. They are not professionals
and do not need group-based access — the relationship is direct,
person-to-person.

### How It Works

When a user authenticates, their Identity resolves via `(issuer, sub)` as
described in the identity architecture. Their effective patient set is:

```
own Person (via PatientUser, if exists)
  + all Person records where they are a PersonalRepresentative
```

A user may have no own PHR and still represent others. A single user can
represent multiple people (e.g. parent with two children) and also manage
their own PHR.

### Joining to Represent Someone Else

```
1. User authenticates via IdP → Identity resolved (get_or_create by issuer, sub)
2. User indicates they are joining to manage someone else's records
3. System creates a new Person for the represented individual
4. PersonalRepresentative record links Identity → new Person
5. User can now upload/modify PHR for that Person
6. Optionally: user also has their own Person record (via PatientUser)
```

---

## Patient Groups

Patients are organized into groups. A group is an arbitrary collection of
patients, defined by the organization for operational purposes:

- Disease cohort (e.g. "Multiple Myeloma patients")
- Location (e.g. "Bay Area clinic")
- Care team (e.g. "Dr. Smith's patients")
- Clinical trial (e.g. "Trial NCT-12345 participants")
- Any other organizational grouping

A patient can belong to multiple groups. A professional can be granted access
to multiple groups.

### Group Membership: Manual and Rule-Based

Group membership can be managed two ways:

**Manual assignment** — a professional with group access adds a patient
explicitly. This is the default for ad-hoc groupings (care teams, clinic
rosters).

**Rule-based auto-assignment** — the customer's host app defines rules that
automatically assign patients to groups based on clinical or demographic
criteria. Examples:

- Diagnosis: patient with ICD-10 C90.0 (Multiple Myeloma) auto-joins
  the "Multiple Myeloma" group
- Location: patient with zip code 94xxx auto-joins "Bay Area" group
- Trial enrollment: patient enrolled in NCT-12345 auto-joins the trial group
- Lab result threshold: patient with eGFR < 60 auto-joins "CKD monitoring"

Rules are defined and executed by the host app, not by HealthKey services.
HealthKey provides the group membership API. The host app calls it when
its rules trigger (on patient creation, diagnosis change, lab result, etc.).

This keeps HealthKey services domain-agnostic — the same way the identity
architecture keeps them IdP-agnostic. The host app owns the business logic
for what constitutes a group and when patients move between groups.
HealthKey services only see the resulting memberships.

### Group Model

```
PatientGroup
  id              — PK
  organization    — FK → Organization
  name            — display name
  slug            — URL-safe identifier
  description     — optional
  rule_managed    — boolean (true if membership is managed by host app rules)
  created_at
  created_by      — FK → Identity (who created the group)

PatientGroupMembership
  group           — FK → PatientGroup
  person          — FK → Person (OMOP)
  source          — enum: manual | rule
  added_at
  added_by        — FK → Identity (NULL when source=rule)

  UNIQUE(group, person)
```

`rule_managed` on PatientGroup signals that the host app controls membership.
Professionals can still view members but should not add/remove manually
(the host app's rules are the source of truth). Groups with
`rule_managed=False` allow manual management by professionals with access.

### Professional Access Grants

```
ProfessionalGroupAccess
  identity        — FK → Identity (the professional)
  group           — FK → PatientGroup
  role            — enum: admin | navigator | doctor
  granted_at
  granted_by      — FK → Identity

  UNIQUE(identity, group)
```

A professional's effective patient set is the union of all patients in all
groups they have access to.

---

## Authorization Logic

Three access paths, checked in order:

### 1. Self-Access (Patient)

```
actor authenticates → resolve Identity (issuer, sub)
Identity → PatientUser → Person
actor.person_id == target_person_id → ALLOW
```

Patients always have full access to their own PHR. No group check needed.

### 2. Personal Representative

```
actor authenticates → resolve Identity (issuer, sub)
PersonalRepresentative.objects.filter(representative=actor, person=target) → ALLOW
```

Same rights as self-access. Used for family members, minors, etc.

### 3. Professional Group Access

```
actor authenticates → resolve Identity (issuer, sub)
actor Identity → ProfessionalGroupAccess → list of group IDs
target_person_id → PatientGroupMembership → list of group IDs
INTERSECT → if non-empty → ALLOW
```

The professional must have at least one group in common with the target
patient. The role on ProfessionalGroupAccess determines what they can do
(currently all professional roles have the same permissions, but the model
supports future differentiation).

### Authorization Check

```python
def can_access_patient(actor_identity: Identity, target_person_id: int) -> bool:
    """Check if actor has access to target patient's data."""
    # 1. Self-access
    try:
        if actor_identity.patient_user.person_id == target_person_id:
            return True
    except PatientUser.DoesNotExist:
        pass

    # 2. Personal representative
    if PersonalRepresentative.objects.filter(
        representative=actor_identity,
        person_id=target_person_id,
    ).exists():
        return True

    # 3. Professional group access
    actor_groups = ProfessionalGroupAccess.objects.filter(
        identity=actor_identity,
    ).values_list('group_id', flat=True)

    return PatientGroupMembership.objects.filter(
        group_id__in=actor_groups,
        person_id=target_person_id,
    ).exists()
```

In integrated mode, every service queries this directly against the
customer's shared database. In standalone mode, each service has the same
tables locally.

---

## Cross-Service Request Flows

These extend the three cross-service communication patterns from the
identity architecture: self-service, on-behalf-of, and service-to-service.

### Self-Service Upload (Patient or Representative)

Follows the identity architecture's self-service pattern. The actor's
`(issuer, sub)` resolves to a Person via `Identity → PatientUser` or
`Identity → PersonalRepresentative`.

```
Host frontend
  | IdP token: iss="...", sub="abc123"
  |
  +-> hk-labs commit → POST to promop /api/lab-results/sync/
  |     Body: { "actor_iss": "...", "actor_sub": "abc123",
  |             "measurements": [...] }
  |
  +-> promop sync endpoint:
        Identity.get_or_create(iss, sub)
        Resolve person from identity (PatientUser or PersonalRepresentative)
        can_access_patient(identity, person_id) → ALLOW (self or representative)
        Record provenance: source=PATIENT_SELF
        Create measurements
```

### On-Behalf-Of Upload (Professional)

Follows the identity architecture's on-behalf-of pattern. The actor's
`(issuer, sub)` identifies the professional, and `person_id` identifies
the target patient.

```
Host frontend
  | IdP token: sub="nav789" (navigator)
  | Target patient: person_id=1042
  |
  +-> hk-labs:
  |     Authenticate actor (Firebase → Identity)
  |     can_access_patient(actor, 1042) → check group intersection
  |     Pass (actor_iss, actor_sub, person_id=1042) to promop
  |
  +-> promop sync endpoint:
        can_access_patient(actor, 1042) → validate (defense in depth)
        Record provenance: source=ADMIN_CORRECTION, actor=nav789, target=1042
        Create measurements for person_id=1042
```

In integrated mode, both hk-labs and promop validate against the same
authorization tables in the customer's shared database. The double-check
is defense in depth, not coordination.

### Rule-Based Group Sync (Host App)

Follows the identity architecture's service-to-service pattern. The host
app manages group membership based on its own business rules.

```
Host app (e.g. ht-phr)
  |
  +- Patient created or diagnosis updated
  +- Host evaluates rules: "ICD-10 C90.0 → group 'multiple-myeloma'"
  |
  +-> POST /api/groups/{id}/members/sync/
        Authorization: Bearer <service-token>
        Body: { "person_ids": [updated list] }
```

HealthKey services are domain-agnostic. The host app owns all business
rules for patient classification. Different host apps can define completely
different grouping criteria.

---

## Invitation Flows

### Professional Creates Patient and Invites

```
1. Professional (admin/navigator/doctor) creates a new Person record
2. Professional assigns Person to one of their groups
3. System generates invitation (email or link)
4. Patient receives invitation → authenticates via IdP → Identity created
5. PatientUser links Identity → Person
6. Patient can now view/modify their own PHR
```

The professional must have group access before they can create patients in
that group.

### Patient Joins Independently

```
1. Patient authenticates via IdP → Identity resolved (get_or_create by issuer, sub)
2. Person auto-provisioned (or matched by email to existing record)
3. PatientUser links Identity → Person
4. Patient uploads/manages own PHR
5. Patient can later invite a professional:
     - System creates ProfessionalGroupAccess if the professional's Identity exists
     - Or generates an invitation link for the professional to claim
```

### Patient Grants Access to Professional

```
1. Patient initiates "grant access" flow
2. Patient selects or invites a professional (by email or link)
3. System creates ProfessionalGroupAccess:
     identity = professional
     group = patient's group (or a new per-patient group is created)
     role = navigator | doctor
     granted_by = patient's Identity
4. Professional can now upload/modify on behalf of that patient
```

When a patient grants access and doesn't belong to an explicit group, the
system creates a personal group (one member: the patient). This keeps the
authorization model uniform — all access goes through groups.

---

## Provenance Recording

Every write records who performed the action. This uses the existing
`ProvenanceRecord` model in promop:

```
ProvenanceRecord
  source              — PATIENT_SELF | ADMIN_CORRECTION | DOCUMENT_EXTRACTION | ...
  source_user_id      — actor's issuer|sub (consistent with identity architecture)
  target_patient_id   — person_id of the patient whose data changed
  modification_reason — optional text
  organization        — FK → Organization
  content_type        — FK → ContentType (what was modified)
  object_id           — PK of the modified record
  created_at
```

| Actor | source | source_user_id |
|---|---|---|
| Patient (self-upload) | `PATIENT_SELF` or `DOCUMENT_EXTRACTION` | patient's `issuer\|sub` |
| Personal representative | `PATIENT_SELF` | representative's `issuer\|sub` |
| Navigator/doctor | `ADMIN_CORRECTION` | professional's `issuer\|sub` |
| Host app (rule sync) | `EHR_SYNC` | service token identifier |

`source_user_id` always uses the `issuer|sub` format from the identity
architecture, making it traceable back to the Identity record in any
service's database.

---

## API Endpoints

```
# Groups
GET    /api/groups/                         — list groups (filtered by actor's access)
POST   /api/groups/                         — create group (admin only)
GET    /api/groups/{id}/                    — group detail + members
POST   /api/groups/{id}/members/            — add patient to group (manual)
DELETE /api/groups/{id}/members/{person_id}/ — remove patient from group

# Rule-managed membership (called by host app via service token)
POST   /api/groups/{id}/members/sync/       — bulk sync members for rule-managed group
  Body: { "person_ids": [1, 2, 3] }
  Adds missing members (source=rule), removes members no longer in the list.
  Only allowed on groups with rule_managed=True.

# Access grants
GET    /api/groups/{id}/access/             — list professionals with access
POST   /api/groups/{id}/access/             — grant professional access
DELETE /api/groups/{id}/access/{identity_id}/ — revoke access

# Personal representatives
GET    /api/representatives/                — list person records the actor represents
POST   /api/representatives/                — add a represented person
DELETE /api/representatives/{person_id}/    — remove representation

# Invitations
POST   /api/invitations/                    — create invitation (patient or professional)
POST   /api/invitations/{token}/accept/     — accept invitation
```

---

## Implementation Notes

### Database Tables

All authorization tables live in the customer's database alongside the
clinical data:

| Table | References | Django App |
|---|---|---|
| `PatientGroup` | Organization | `omop_core` |
| `PatientGroupMembership` | PatientGroup, Person | `omop_core` |
| `ProfessionalGroupAccess` | Identity, PatientGroup | `omop_core` |
| `PersonalRepresentative` | Identity, Person | `omop_core` |

These tables reference both `Identity` (from the auth layer) and `Person`
/ `Organization` (from the clinical layer). They belong to `omop_core`
because that's where Person and Organization are defined.

In standalone mode for hk-labs (when not connected to promop), equivalent
tables would live in the `accounts` app with simplified models.

### Relationship to Identity Architecture

| Identity Architecture Concept | Role Model Usage |
|---|---|
| `Identity (issuer, sub)` | FK on all authorization tables (who has access) |
| `PatientUser (Identity → Person)` | Self-access check in `can_access_patient()` |
| `TokenClaims` | Read at request time for `source_user_id` in provenance |
| `PartnerAuthentication` | Resolves actor Identity before authorization check |
| Service tokens | Host app uses service token for rule-based group sync |
| Cross-service `(issuer, sub)` | Actor identity passed in sync payloads |

---

## Future Considerations

- **Permission differentiation by role**: Currently admin/navigator/doctor
  have identical write permissions. The model supports adding granular
  permissions (e.g. doctor can modify clinical notes, navigator cannot).

- **Time-limited access**: Add `expires_at` to ProfessionalGroupAccess for
  temporary grants (clinical trial duration, consult period).

- **Audit log**: ProfessionalGroupAccess changes (grant/revoke) should be
  logged for compliance.

- **Group hierarchy**: Groups could nest (e.g. "All Bay Area" contains
  "Bay Area Clinic A" and "Bay Area Clinic B"). Not needed initially.

- **FHIR Consent**: Map patient access grants to FHIR Consent resources
  for interoperability with external EHR systems.

- **healthkey-identity library**: When the shared identity library ships
  (see identity architecture), authorization helpers (`can_access_patient`,
  group management) should be included or packaged alongside it.
