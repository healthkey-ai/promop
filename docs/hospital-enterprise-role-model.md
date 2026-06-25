# Role Model & Access Control (v2.0)

Hospital enterprise role model 

## Overview

This document defines how HealthKey services authorize access to patient data. It builds on the [Identity Architecture](https://www.google.com/search?q=identity-architecture.md), which covers authentication (who you are). This document covers authorization (what you can do).

The architecture utilizes a hybrid model combining **Role-Based Access Control (RBAC)** for functional permissions and **Attribute-Based/Contextual Access Control (ABAC)** for patient data boundaries. It is designed to mirror the strict data governance, auditing, and emergency override workflows required by enterprise hospital networks and compliance frameworks (e.g., HIPAA, 42 CFR Part 2).

The customer owns all patient data and the database it lives in. HealthKey services are software the customer deploys to manage that data on the customer's behalf. Authorization tables live in the same customer-owned database as the clinical data.

### Two Operating Modes

Authorization works the same way in both modes defined in the identity architecture:

| Mode | Database | Identity Source | Authorization Tables |
| --- | --- | --- | --- |
| **Standalone** | Service-local database, owned by the customer | Local identities (`iss="urn:local"`) | In the service's own database |
| **Integrated** | Customer's shared database, all services connected | Customer's IdP (e.g. Firebase) | In the customer's shared database, queryable by all services |

In integrated mode, the customer provides a single database that all HealthKey services connect to. Authorization tables (groups, memberships, access grants) live alongside the clinical data. Any service can run `can_access_patient()` directly against the shared database — no cross-service API calls needed for access checks.

Switching between modes is a settings change (same as identity). The authorization schema is identical in both modes.

---

## Functional Roles & Functional Permissions

To protect clinical integrity and enforce a strict separation of duties, application actions are governed by specific functional permissions tied to roles. Users are assigned a primary role within their organizational scope.

### Core Roles & Matrix

| Role | Manage Groups / Grants | Edit Clinical Records (Sign Off) | Upload / Modify Administrative Records | Manage Patient Invites | Scope |
| --- | --- | --- | --- | --- | --- |
| **admin** | Yes | No | Yes (on behalf of group patients) | Yes | All patients in assigned groups |
| **doctor** | No | Yes (on behalf of group patients) | Yes (on behalf of group patients) | No | Patients in assigned groups |
| **navigator** | No | No | Yes (on behalf of group patients) | No | Patients in assigned groups |
| **patient** | No | No | Yes (own + represented persons) | Yes (grant access to professionals) | Own data + personal representatives |

### Role Semantics

* **admin** — Organization administrator. Manages operational infrastructure. Full authority to create patient groups, assign patients to groups, and manage professional access grants. They hold administrative access but do not write or sign off on clinical records.
* **doctor** — Licensed clinician. Possesses absolute clinical write privileges. Authorized to sign off on clinical diagnoses, prescriptions, treatment charts, and medical assessments.
* **navigator** — Patient navigator / care coordinator. Assists with administrative operations. Authorized to upload lab receipts, arrange schedules, and manage demographic records. Strictly restricted from signing off on or mutating core clinical assessments or medical charts.
* **patient** — The data owner. Can upload and modify their own personal health records (PHR). Can join the system independently or be invited by a professional. Can grant access to professionals by creating a bounded patient group.

A patient may also act as a **personal representative** (proxy) for other individuals (e.g., a minor child or an elderly parent). This requires formal verification as detailed under the Personal Representatives section.

---

## Personal Representatives

A user joining as a patient may not be managing only their own health records. Common scenarios include:

* Parent managing a minor child's PHR
* Adult child managing an elderly parent's PHR
* Spouse or partner managing records for a family member
* Friend or caregiver helping someone who cannot self-manage

### Model

```
PersonalRepresentative
  id                        — PK
  representative            — FK → Identity (the proxy manager)
  person                    — FK → Person (whose PHR is being managed)
  relationship              — enum: parent, child, spouse, legal_guardian, caregiver, other
  verification_status       — enum: PENDING | VERIFIED | REJECTED (default: PENDING)
  verified_at               — datetime (nullable)
  verified_by               — FK → Identity (the admin or clerk who checked legal proxy documents)
  granted_at
  granted_by                — FK → Identity (who authorized this: self, the patient, or an admin)

  UNIQUE(representative, person)

```

A personal representative has the same rights as the patient themselves to upload, modify, and view the represented person's PHR once verified. They are not professionals and do not need group-based access — the relationship is direct and person-to-person. To meet enterprise compliance standards, proxy relationships require data verification by an administrative user to confirm legal status (e.g., power of attorney or birth certificates).

### How It Works

When a user authenticates, their Identity resolves via `(issuer, sub)` as described in the identity architecture. Their effective patient set is:

```
own Person (via PatientUser, if exists)
  + all Person records where they are a VERIFIED PersonalRepresentative

```

A user may have no own PHR and still represent others. A single user can represent multiple people (e.g., a parent with two children) and also manage their own PHR.

### Joining to Represent Someone Else

```
1. User authenticates via IdP → Identity resolved (get_or_create by issuer, sub)
2. User indicates they are joining to manage someone else's records
3. System creates a new Person for the represented individual
4. PersonalRepresentative record links Identity → new Person (verification_status="PENDING")
5. Admin verifies legal proxy documentation → status updated to "VERIFIED"
6. User can now upload/modify PHR for that Person
7. Optionally: user also has their own Person record (via PatientUser)

```

---

## Patient Groups

Patients are organized into groups. A group is an arbitrary collection of patients, defined by the organization for operational purposes:

* Disease cohort (e.g., "Multiple Myeloma patients")
* Location (e.g., "Bay Area clinic")
* Care team (e.g., "Dr. Smith's patients")
* Clinical trial (e.g., "Trial NCT-12345 participants")
* Any other organizational grouping

A patient can belong to multiple groups. A professional can be granted access to multiple groups.

### Group Membership: Manual and Rule-Based

Group membership can be managed two ways:

**Manual assignment** — a professional with group access adds a patient explicitly. This is the default for ad-hoc groupings (care teams, clinic rosters).

**Rule-based auto-assignment** — the customer's host app defines rules that automatically assign patients to groups based on clinical or demographic criteria. Examples:

* **Diagnosis:** patient with ICD-10 C90.0 (Multiple Myeloma) auto-joins the "Multiple Myeloma" group
* **Location:** patient with zip code 94xxx auto-joins "Bay Area" group
* **Trial enrollment:** patient enrolled in NCT-12345 auto-joins the trial group
* **Lab result threshold:** patient with eGFR < 60 auto-joins "CKD monitoring"

Rules are defined and executed by the host app, not by HealthKey services. HealthKey provides the group membership API. The host app calls it when its rules trigger (on patient creation, diagnosis change, lab result, etc.).

This keeps HealthKey services domain-agnostic — the same way the identity architecture keeps them IdP-agnostic. The host app owns the business logic for what constitutes a group and when patients move between groups. HealthKey services only see the resulting memberships.

### Group Model

```
PatientGroup
  id                        — PK
  organization              — FK → Organization
  name                      — display name
  slug                      — URL-safe identifier
  description               — optional
  rule_managed              — boolean (true if membership is managed by host app rules)
  created_at
  created_by                — FK → Identity (who created the group)

PatientGroupMembership
  group                     — FK → PatientGroup
  person                    — FK → Person (OMOP)
  source                    — enum: manual | rule
  added_at
  added_by                  — FK → Identity (NULL when source=rule)

  UNIQUE(group, person)

```

`rule_managed` on PatientGroup signals that the host app controls membership. Professionals can still view members but should not add/remove manually (the host app's rules are the source of truth). Groups with `rule_managed=False` allow manual management by professionals with access.

### Professional Access Grants

```
ProfessionalGroupAccess
  identity                  — FK → Identity (the professional)
  group                     — FK → PatientGroup
  role                      — enum: admin | doctor | navigator
  max_sensitivity_clearance — enum: NORMAL | RESTRICTED | RESTRICTED_VIP (default: NORMAL)
  granted_at
  granted_by                — FK → Identity
  expires_at                — datetime (optional, for time-bound care or external consults)

  UNIQUE(identity, group)

```

A professional's effective patient set is the union of all patients in all groups they have access to, limited by expiration limits and data sensitivity clearance levels.

---

## Data Segmentation & Sensitivity Control

Enterprise medical networks manage distinct classes of health data requiring varied levels of protection. To prevent unauthorized exposure of highly sensitive information, both patients and data items are classified using **Sensitivity Levels**.

### Sensitivity Levels

* `NORMAL`: Standard clinical data (vitals, general medicine encounters, routine labs).
* `RESTRICTED`: Highly sensitive data protected by explicit legal frameworks (e.g., behavioral/mental health notes, substance use disorder treatments under 42 CFR Part 2, or genetic screenings).
* `RESTRICTED_VIP`: Highly protected demographic status applied to high-profile individuals, celebrities, or staff members under active monitoring.

### Enforcement Rule

A professional matching a patient group intersection can view `NORMAL` data. To access `RESTRICTED` or `RESTRICTED_VIP` data elements, the professional's `ProfessionalGroupAccess` entry must possess an explicit `max_sensitivity_clearance` matching or exceeding the data's classification level.

---

## Emergency Access Control ("Break-Glass")

In critical clinical scenarios (such as emergency room triage or unexpected coverage shifts), a clinician may require immediate access to a patient chart outside their assigned groups. The architecture provides a secure **Break-Glass** mechanism to override normal group restrictions safely.

### The Emergency Workflow

```
[Request Access] ---> (Check Standard Group & Sensitivity Rules)
                             |
                             +---> ALLOWED -> [Render Chart]
                             |
                             +---> DENIED  -> [Prompt Break-Glass Override]
                                                    |
                                                    +---> User Aborts -> [Exit]
                                                    +---> User Submits Justification
                                                                |
                                                                v
                                                [Log Immutable Break-Glass Event]
                                                                |
                                                                v
                                                [Provision 24-Hour Active Token]
                                                                |
                                                                v
                                                          [Render Chart]

```

### Break-Glass Guardrails

1. **Strictly Bounded:** A Break-Glass activation grants the individual clinician access to that single target patient's chart for a hard-coded window of **24 hours**.
2. **Explicit Justification Required:** The clinician must submit a formal textual reason (e.g., *"Emergency room admission, patient unconscious"*).
3. **High-Priority Audit Trigger:** The activation instantly publishes a critical event to the compliance framework, flagging the entry for mandatory manual review by compliance officers.
4. **Sensitivity Restrictions:** Standard Break-Glass overrides grant access to `NORMAL` data elements. Overriding a `RESTRICTED_VIP` status requires a specialized administrative flag.

### Emergency Override Model

```
BreakGlassActivation
  id                        — PK
  identity                  — FK → Identity (the clinician executing the override)
  person                    — FK → Person (the patient being accessed)
  justification             — text (mandatory clinical reason)
  activated_at              — datetime
  expires_at                — datetime (set exactly to activated_at + Hardcoded 24 Hours)
  reviewed_by_compliance    — boolean (default: False)

```

---

## Authorization & Access Validation Logic

Four access paths, checked in order:

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
PersonalRepresentative.objects.filter(representative=actor, person=target, verification_status="VERIFIED") → ALLOW

```

Same rights as self-access once confirmed by administrative documentation review.

### 3. Emergency Break-Glass Override

```
actor authenticates → resolve Identity (issuer, sub)
BreakGlassActivation.objects.filter(identity=actor, person=target, expires_at__gt=now) → ALLOW

```

Temporary 24-hour bypass containing an enforced justification payload.

### 4. Professional Group Access

```
actor authenticates → resolve Identity (issuer, sub)
actor Identity → ProfessionalGroupAccess (valid context, clearance matching sensitivity) → list of group IDs
target_person_id → PatientGroupMembership → list of group IDs
INTERSECT → if non-empty → ALLOW

```

### Computational Access Function

```python
from django.utils import timezone
from django.core.exceptions import PermissionDenied

def can_access_patient(
    actor_identity: Identity, 
    target_person_id: int, 
    required_sensitivity: str = "NORMAL"
) -> bool:
    """
    Evaluates system access control restrictions.
    Returns True if permitted, False or raises PermissionDenied if blocked.
    """
    now = timezone.now()

    # PATH 1: Self-Access (Direct Patient Owner)
    try:
        if actor_identity.patient_user.person_id == target_person_id:
            # Patients possess universal clearance to their own personal files
            return True
    except PatientUser.DoesNotExist:
        pass

    # PATH 2: Personal Representative (Proxy) Access
    # Real health networks mandate that proxy relationships must pass manual legal document verification
    if PersonalRepresentative.objects.filter(
        representative=actor_identity,
        person_id=target_person_id,
        verification_status="VERIFIED"
    ).exists():
        return True

    # PATH 3: Emergency Break-Glass Override Check
    has_active_break_glass = BreakGlassActivation.objects.filter(
        identity=actor_identity,
        person_id=target_person_id,
        expires_at__gt=now
    ).exists()

    if has_active_break_glass:
        # Enforce baseline sensitivity constraints on overrides unless target is VIP
        if required_sensitivity == "RESTRICTED_VIP":
            raise PermissionDenied("Break-glass token insufficient for VIP clinical records.")
        return True

    # PATH 4: Professional Group Access
    # Query all active groups where the professional holds valid membership
    active_professional_grants = ProfessionalGroupAccess.objects.filter(
        identity=actor_identity
    ).filter(
        models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now)
    )

    # Filter grants capable of handling the record's sensitivity demands
    if required_sensitivity == "RESTRICTED_VIP":
        allowed_grants = active_professional_grants.filter(max_sensitivity_clearance="RESTRICTED_VIP")
    elif required_sensitivity == "RESTRICTED":
        allowed_grants = active_professional_grants.filter(
            max_sensitivity_clearance__in=["RESTRICTED", "RESTRICTED_VIP"]
        )
    else:
        allowed_grants = active_professional_grants # NORMAL clearance handles baseline items

    # Match group memberships against the targets assigned buckets
    actor_group_ids = allowed_grants.values_list('group_id', flat=True)
    
    is_member = PatientGroupMembership.objects.filter(
        group_id__in=actor_group_ids,
        person_id=target_person_id
    ).exists()

    return is_member

```

In integrated mode, every service queries this directly against the customer's shared database. In standalone mode, each service has the same tables locally.

---

## Cross-Service Request Flows

These extend the three cross-service communication patterns from the identity architecture: self-service, on-behalf-of, and service-to-service.

### Self-Service Upload (Patient or Representative)

Follows the identity architecture's self-service pattern. The actor's `(issuer, sub)` resolves to a Person via `Identity → PatientUser` or `Identity → PersonalRepresentative` (requires verified status).

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
        Resolve person from identity (PatientUser or verified PersonalRepresentative)
        can_access_patient(identity, person_id) → ALLOW (self or representative)
        Record provenance: source=PATIENT_SELF
        Create measurements

```

### On-Behalf-Of Upload (Professional)

Follows the identity architecture's on-behalf-of pattern. The actor's `(issuer, sub)` identifies the professional, and `person_id` identifies the target patient.

```
Host frontend
  | IdP token: sub="nav789" (navigator)
  | Target patient: person_id=1042
  |
  +-> hk-labs:
  |     Authenticate actor (Firebase → Identity)
  |     can_access_patient(actor, 1042) → check group intersection / clearance
  |     Pass (actor_iss, actor_sub, person_id=1042) to promop
  |
  +-> promop sync endpoint:
        can_access_patient(actor, 1042) → validate (defense in depth)
        Record provenance: source=ADMIN_CORRECTION, actor=nav789, target=1042
        Create measurements for person_id=1042

```

In integrated mode, both hk-labs and promop validate against the same authorization tables in the customer's shared database. The double-check is defense in depth, not coordination.

### Rule-Based Group Sync (Host App)

Follows the identity architecture's service-to-service pattern. The host app manages group membership based on its own business rules.

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

HealthKey services are domain-agnostic. The host app owns all business rules for patient classification. Different host apps can define completely different grouping criteria.

---

## Invitation Flows

### Professional Creates Patient and Invites

```
1. Professional (admin/doctor) creates a new Person record
2. Professional assigns Person to one of their groups
3. System generates invitation (email or link)
4. Patient receives invitation → authenticates via IdP → Identity created
5. PatientUser links Identity → Person
6. Patient can now view/modify their own PHR

```

The professional must have group access before they can create patients in that group.

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
     role = doctor | navigator
     granted_by = patient's Identity
4. Professional can now upload/modify on behalf of that patient based on their functional role

```

When a patient grants access and doesn't belong to an explicit group, the system creates a personal group (one member: the patient). This keeps the authorization model uniform — all access goes through groups.

---

## Comprehensive Security Auditing

Enterprise healthcare regulations mandate that security architectures must track not only *mutations* but also exactly *who looked at what data elements*. The platform combines modification provenance with data access logs.

### 1. Data Modification Tracking (Provenance)

Every write records who performed the action. This uses the existing `ProvenanceRecord` model in promop:

```
ProvenanceRecord
  id                  — PK
  source              — PATIENT_SELF | ADMIN_CORRECTION | CLINICAL_SIGN_OFF | EHR_SYNC
  source_user_id      — actor's issuer|sub (consistent with identity architecture)
  target_patient_id   — person_id of the patient whose data changed
  modification_reason — optional text
  organization        — FK → Organization
  content_type        — FK → ContentType (what was modified)
  object_id           — PK of the modified record
  created_at          — datetime

```

| Actor | source | source_user_id |
| --- | --- | --- |
| Patient (self-upload) | `PATIENT_SELF` | patient's `issuer|sub` |
| Personal representative | `PATIENT_SELF` | representative's `issuer|sub` |
| Doctor | `CLINICAL_SIGN_OFF` or `ADMIN_CORRECTION` | professional's `issuer|sub` |
| Navigator | `ADMIN_CORRECTION` | professional's `issuer|sub` |
| Host app (rule sync) | `EHR_SYNC` | service token identifier |

`source_user_id` always uses the `issuer|sub` format from the identity architecture, making it traceable back to the Identity record in any service's database.

### 2. Data Viewing Auditing (Access Logs)

To satisfy clinical audit trails, every read transaction generates an unalterable access log tracking the explicit viewing context.

```
DataAccessLog
  id                  — PK
  identity            — FK → Identity (who viewed the data)
  person              — FK → Person (the patient chart that was displayed)
  accessed_at         — datetime
  endpoint_context    — string (e.g., "/api/lab-results/detail/")
  override_active     — boolean (True if accessed via an active Break-Glass window)

```

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
POST   /api/groups/{id}/access/             — grant professional access (specifying sensitivity levels)
DELETE /api/groups/{id}/access/{identity_id}/ — revoke access

# Personal representatives (Proxy Verification Interface)
GET    /api/representatives/                — list person records the actor represents
POST   /api/representatives/                — add a represented person (sets status to PENDING)
POST   /api/representatives/{id}/verify/    — verify proxy legal documentation (admin only)
DELETE /api/representatives/{person_id}/    — remove representation

# Emergency Break-Glass Interface
POST   /api/break-glass/activate/           — activate emergency access token
  Body: { "person_id": 1042, "justification": "Emergency scenario description..." }

# Compliance Audit Logging
GET    /api/compliance/audit-logs/          — extract access history records (compliance officer role required)

```

---

## Implementation Notes

### Database Tables

All authorization tables live in the customer's database alongside the clinical data:

| Table | References | Django App |
| --- | --- | --- |
| `PatientGroup` | Organization | `omop_core` |
| `PatientGroupMembership` | PatientGroup, Person | `omop_core` |
| `ProfessionalGroupAccess` | Identity, PatientGroup | `omop_core` |
| `PersonalRepresentative` | Identity, Person | `omop_core` |
| `BreakGlassActivation` | Identity, Person | `omop_core` |
| `DataAccessLog` | Identity, Person | `omop_core` |

These tables reference both `Identity` (from the auth layer) and `Person` / `Organization` (from the clinical layer). They belong to `omop_core` because that's where Person and Organization are defined.

In standalone mode for hk-labs (when not connected to promop), equivalent tables would live in the `accounts` app with simplified models.

### Relationship to Identity Architecture

| Identity Architecture Concept | Role Model Usage |
| --- | --- |
| `Identity (issuer, sub)` | FK on all authorization tables (who has access) |
| `PatientUser (Identity → Person)` | Self-access check in `can_access_patient()` |
| `TokenClaims` | Read at request time for `source_user_id` in provenance and access logs |
| `PartnerAuthentication` | Resolves actor Identity before authorization check |
| Service tokens | Host app uses service token for rule-based group sync |
| Cross-service `(issuer, sub)` | Actor identity passed in sync payloads |

---

## Future Considerations

* **Time-limited access**: Add `expires_at` to ProfessionalGroupAccess for temporary grants (clinical trial duration, consult period). *[Partially addressed in structural schema updates]*
* **Group hierarchy**: Groups could nest (e.g., "All Bay Area" contains "Bay Area Clinic A" and "Bay Area Clinic B"). Not needed initially.
* **FHIR Consent**: Map patient access grants to FHIR Consent resources for interoperability with external EHR systems.
* **healthkey-identity library**: When the shared identity library ships (see identity architecture), authorization helpers (`can_access_patient`, group management) should be included or packaged alongside it.