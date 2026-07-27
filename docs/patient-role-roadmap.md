# Patient Role & Org-Scoped Access — Implementation Roadmap

## Goal

Add a first-class **patient** role to the org/access system so that:
1. Org admins can **invite patients by email**, optionally tying the invite to an existing `PatientRecord`
2. Orgs can **opt-in to direct patient signup** (self-registration)
3. Patients **log in at an org-scoped URL** (`/<basename>/org/<slug>`) and land directly on their own record
4. Invitation emails for all roles (patient, doctor, analyst) include the org URL

---

## Phase 1 — Model & Backend Changes

### 1.1 Add `allows_patient_signup` to `Organization`

```python
# omop_core/models.py – Organization
allows_patient_signup = models.BooleanField(
    default=False,
    help_text="When true, patients may self-register via the org's public page.",
)
```

- Settable by Org Admins, superusers, and staff (add to `OrgSerializer` writable fields).
- Migration: `makemigrations omop_core`.

### 1.2 Add `patient` to `GroupAccess.ROLE_CHOICES` and `OrgInvitation.ROLE`

```python
ROLE_CHOICES = [
    ('org_admin', 'Org Admin'),
    ('doctor',    'Doctor'),
    ('analyst',   'Analyst'),
    ('patient',   'Patient'),
]
```

- A `patient` `GroupAccess` row ties an Identity to an org (like other roles) but grants no provider-level permissions.
- Update `OrgInvitation.ROLE` to match.

### 1.3 Extend `OrgInvitation` for patient-specific fields

```python
# Optional FK — lets the admin tie the invite to a specific PatientRecord/Person
person = models.ForeignKey(
    'Person', on_delete=models.SET_NULL,
    null=True, blank=True,
    help_text="If set, accepting the invite links the patient to this person record.",
)
```

### 1.4 Patient invitation flow (backend)

When an Org Admin sends an invite with `role=patient`:

1. Create `OrgInvitation` with `role='patient'` and optional `person_id`.
2. If no `Identity` exists for the email, create a placeholder (same as today's org invite flow).
3. Email the invite with a link to the **org-scoped URL**: `{APP_BASE_URL}/org/{slug}/accept-invite?token=...`.
4. On acceptance:
   - If `person` was set on the invite → create `PatientUser(identity, person)` linking them.
   - If `person` was not set → create a new `Person` + `PatientRecord` + `PatientUser`.
   - Create `GroupAccess(identity, org, role='patient')`.

### 1.5 Patient self-signup flow (backend)

New endpoint: `POST /api/v1/orgs/{slug}/patient-signup/`

- **Public** (AllowAny) — only if `org.allows_patient_signup` is `True`.
- Accepts `email`, `password`, `given_name`, `family_name`.
- Creates `Identity`, `Person`, `PatientRecord`, `PatientUser`, and `GroupAccess(role='patient')`.
- Returns 201 + sets session (auto-login).
- Rate-limit (throttle class) to prevent abuse.

### 1.6 Update `patient_person_for()` logic

Currently: "is patient" = has `PatientUser` + zero `GroupAccess` + not staff.

After: "is patient" = has `PatientUser` + (zero `GroupAccess` **OR** only `GroupAccess` rows with `role='patient'`) + not staff.

This ensures that a patient who was invited into an org (and therefore has a `GroupAccess`) is still recognized as a patient, not a provider.

### 1.7 Scope patient queries to org

Patients with a `GroupAccess(role='patient')` should only see their own record within the org context. The existing `PatientSelfScopePermission` already restricts patients to their own `person_id` — no change needed there. But the `/api/v1/user/` response should include the patient's org slug(s) so the frontend can build org-scoped URLs.

---

## Phase 2 — Frontend: Org-Scoped URLs & Patient Pages

### 2.1 Org-scoped URL routing

Add routes under `/org/:slug/`:

```
/org/:slug/login             → OrgLogin (patient login for this org)
/org/:slug/signup            → OrgPatientSignup (only if org.allows_patient_signup)
/org/:slug/accept-invite     → OrgAcceptInvite (patient invite acceptance)
/org/:slug/                  → patient lands on own record (PatientHome)
```

The `OrgLogin` page:
- Fetches org details via `GET /api/v1/orgs/{slug}/public/` (new lightweight public endpoint returning org name and `allows_patient_signup`).
- Shows the org name/branding.
- Has email + password login form.
- If `allows_patient_signup`, shows a "Sign Up" link to `/org/:slug/signup`.

### 2.2 Patient landing page

When a patient logs in at `/org/:slug/login`, on success redirect to `/org/:slug/` which renders `PatientHome` — their own patient record detail. No patient list. No navigation to other patients.

### 2.3 Org-scoped invite acceptance

`/org/:slug/accept-invite?token=...` — same as current `AcceptInvite` but:
- Shows org branding (name from the token's org).
- For patient invites, creates the account and links the person record.
- After success, redirects to `/org/:slug/` (their record).

### 2.4 Org-scoped signup page

`/org/:slug/signup` — a registration form (email, password, name) that calls `POST /api/v1/orgs/{slug}/patient-signup/`. Only rendered if the org allows direct signup.

### 2.5 Update invitation emails

All invite emails (patient, doctor, analyst) should include the org-scoped URL:

- Patient invite: `{APP_BASE_URL}/org/{slug}/accept-invite?token=...`
- Doctor/analyst invite: `{APP_BASE_URL}/org/{slug}/accept-invite?token=...`

After accepting, doctors/analysts are redirected to the main app (`/`) while patients are redirected to `/org/{slug}/`.

---

## Phase 3 — Org Admin UI for Patient Invitations

### 3.1 Patient invite form in Org Admin page

Add a "Patients" tab or section to `OrgAdminPage` with:
- **Invite Patient** button → modal/form:
  - Email (required)
  - Tie to existing patient record (optional) — searchable dropdown of `Person` records in the org
  - Send button
- List of pending/confirmed patient invitations

### 3.2 Org settings: `allows_patient_signup` toggle

In Org Admin settings, add a toggle for "Allow direct patient signup". Requires `org_admin`, staff, or superuser.

---

## Phase 4 — Tests

### Backend tests

- Model tests: `GroupAccess` with `role='patient'`, `Organization.allows_patient_signup`
- `patient_person_for()` with patient role `GroupAccess` — still resolves as patient
- Patient self-signup endpoint: happy path, org with signup disabled (403), duplicate email
- Patient invite: create invite with/without `person_id`, accept flow, token expiry
- Org-scoped public endpoint returns correct fields
- Permission tests: patient cannot access other patients' records, cannot access provider routes

### Frontend tests

- `OrgLogin` renders, shows signup link only when enabled
- `OrgPatientSignup` form submission
- Patient redirect to own record after login
- Invite acceptance flow with org context

---

## Phase 5 — Migration & Rollout

1. Deploy backend (model + migration + endpoints) first.
2. Deploy frontend with new routes.
3. Existing `PatientInvitation` flow continues to work (no breaking changes). Over time, migrate to the org-scoped invite path.
4. The existing `/accept-patient-invite` route remains as a fallback; new invites use `/org/{slug}/accept-invite`.

---

## Open Questions

1. **Patient branding per org** — Should orgs have a logo/color stored for the login page? Defer to a future phase unless needed now.
2. **Multiple org membership** — A patient could belong to multiple orgs. The current design supports this (multiple `GroupAccess` rows). The patient would need an org picker or land on the most recent org. Recommend: defer, land on the org from their login URL.
3. **Existing `PatientInvitation` model** — There's already a separate `PatientInvitation` model (in `patient_portal/models.py`). The new patient invite flow uses `OrgInvitation` with `role='patient'` instead. Keep `PatientInvitation` for backwards compatibility; deprecate over time.
