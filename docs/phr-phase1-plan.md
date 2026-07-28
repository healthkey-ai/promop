# HL7 PHR System Functional Model R2 Support — Phase 1

> **Status:** Plan under review
> **Branch:** `feature/phr-phase1`
> **Original evaluation:** 13 conformant / 76 partial / 128 non-conformant
> **Spec:** https://hl7.org/fhir/uv/phr/2025May

---

## Problem Statement

promop is a **provider-centric** oncology record system. Patients can authenticate and view their own data, but they cannot act as **account holders** who manage access, delegate to representatives, enforce consent, or audit who has seen their record. This plan establishes the patient as a first-class account holder — the foundational change required to move from provider-EHR semantics toward PHR semantics.

---

## Review Notes & Architectural Decisions

### Critical Issues Found

#### 1. **PatientProviderGrant creates dual access paths (HIGH)**

The plan creates `PatientProviderGrant` as a parallel system to `GroupAccess`. Both grant providers access to patient data. The difference:
- `GroupAccess` — org-admin grants access to a group of patients
- `PatientProviderGrant` — patient grants access to a specific provider

**Problem:** `can_access_patient()` and `can_write_patient()` now need to check BOTH systems. This is error-prone and creates ambiguity about which access path "wins" when they conflict.

**Decision:** Keep `PatientProviderGrant` but:
- On activation (provider accepts), it creates a `GroupAccess` row with `granted_by_patient = True` and a synthetic one-patient group
- `can_access_patient()` only checks `GroupAccess` (single source of truth)
- `PatientProviderGrant` becomes the **request/negotiation** layer, not the **enforcement** layer
- Add `GroupAccess.granted_by_patient` boolean to distinguish patient-initiated vs org-initiated

#### 2. **Audit logging every read will kill performance (HIGH)**

The plan proposes logging ALL reads (GET/HEAD/OPTIONS) to the DB via middleware. In a busy system with providers paging through patient lists, this could generate 10-100x more audit rows than clinical data rows.

**Decision:**
- Log **writes** (POST/PUT/PATCH/DELETE) synchronously to `AccessAuditLog`
- Log **reads** to `AccessAuditLog` ONLY for:
  - Patient record detail views (`/patient-records/<id>/`)
  - FHIR downloads
  - Data export operations
- Use the existing stdout audit logger for operational observability of all requests
- Do NOT log list views (`/patient-records/`) to the DB — too noisy
- Future: add a `BatchAuditLog` table for high-volume reads with periodic flush

#### 3. **Consent bypass for institutional access is ethically wrong (MEDIUM)**

The plan says "Org-scoped GroupAccess bypasses consent (institutional clinical access)." In a true PHR, the patient controls ALL access. However, clinicians need access to treat patients.

**Decision:** Compromise:
- Patient can revoke ANY access, including institutional
- BUT revocation does not take effect until `revocation_effective_date` (default 24h for clinical safety)
- Emergency override: `GroupAccess` with `role='emergency'` bypasses consent but creates an immediate alert audit entry
- Display pending revocations clearly to both patient and provider

#### 4. **Feature flag mentioned but not implemented (MEDIUM)**

The plan says `PHR_CONSENT_ENFORCED=True` but doesn't specify where or how.

**Decision:** Add `PHR_CONSENT_ENFORCED` to `settings.py` (default `False` during transition). When `False`:
- Consent records are stored but NOT enforced
- Audit log is written but not queryable by patients yet
- All new APIs work but old behavior is preserved

#### 5. **Missing: invitation acceptance flow (HIGH)**

The plan mentions invitation tokens and email but has no acceptance URL, view, or flow.

**Decision:** Add to plan:
- `GET /api/v1/invitations/accept/<token>/` — validates token, creates Identity link if needed, sets `verification_status='VERIFIED'`
- Email template (Django template) for invitation
- Frontend route `/accept-invitation/<token>`

#### 6. **django-simple-history on all clinical tables is overkill (MEDIUM)**

The plan proposes `HistoricalRecords()` on ConditionOccurrence, DrugExposure, Measurement, Observation, ProcedureOccurrence, PatientRecord. These are OMOP tables that could have millions of rows. Each history table doubles storage.

**Decision:**
- Start with `PatientRecord` only (the read model patients actually see)
- Defer clinical table history to Phase 2 after storage analysis
- Use `ProvenanceRecord` (already exists) for clinical write audit trail

#### 7. **MyAccessViewSet "unified view" is too complex (MEDIUM)**

`GET /api/v1/my-access/` returning four different data types in one response is hard to cache, paginate, and type.

**Decision:** Keep separate endpoints:
- `GET /api/v1/my-access/` → returns metadata only (patient person_id, counts)
- `GET /api/v1/my-access/representatives/` → paginated list
- `GET /api/v1/my-access/providers/` → paginated list
- `GET /api/v1/my-access/professional/` → read-only list of org-level access
- `GET /api/v1/my-access/audit/` → paginated audit log (with date filters)

#### 8. **Token generation must be cryptographically secure (MEDIUM)**

The plan mentions `invitation_token` but doesn't specify how to generate it.

**Decision:** Use `secrets.token_urlsafe(32)` (not `uuid4()`, not `random`). Document this explicitly.

#### 9. **No rate limiting on invitations (MEDIUM)**

Patients could spam invitation creation.

**Decision:** Add per-user rate limiting:
- Max 10 invitations per day per patient
- Max 3 pending invitations per patient
- Return 429 with `Retry-After` header

#### 10. **FHIR export is underspecified (MEDIUM)**

"Reuse generate_fhir_bundle.py logic" — but that file generates synthetic data, not export existing data.

**Decision:**
- Create new `patient_portal/api/export.py` module
- Query OMOP tables for the patient and build FHIR Bundle resources
- Reuse resource-building patterns from `generate_fhir_bundle.py` but not the random data logic
- Add `ProvenanceRecord` with `source='PATIENT_SELF'` for the export event

---

## Revised Implementation Order

### Phase 1A: Foundation (Patient Access & Representatives)

**PR: `feature/phr-phase1-access`**

#### 1. Extend `PersonalRepresentative` model
- **File:** `omop_core/models.py`
- Add invitation/revocation fields:
  - `invited_by` — FK to Identity (nullable, related_name='sent_representative_invitations')
  - `invitation_token` — CharField(64, unique, nullable, db_index=True)
  - `invitation_email` — CharField(255, blank)
  - `invitation_expires_at` — DateTimeField(nullable)
  - `revoked_at` — DateTimeField(nullable)
  - `revoked_by` — FK to Identity(nullable, related_name='+')
  - `notes` — TextField(blank)
- **Token generation:** `secrets.token_urlsafe(32)`
- **Migration:** `makemigrations omop_core`

#### 2. Add `granted_by_patient` to `GroupAccess`
- **File:** `omop_core/models.py`
- Add `granted_by_patient = models.BooleanField(default=False)`
- This distinguishes patient-initiated provider access from org-admin grants
- **Migration:** `makemigrations omop_core`

#### 3. New `PatientProviderGrant` model (negotiation layer)
- **File:** `omop_core/models.py`
- Status: pending / active / revoked / expired / rejected
- Access levels: full / read_only / messaging
- On activation: creates `GroupAccess` with `granted_by_patient=True`
- On revocation: sets `GroupAccess.expires_at = now()`
- **Migration:** `makemigrations omop_core`

#### 4. New `AccessAuditLog` model
- **File:** `omop_core/models.py`
- Same fields as original plan
- **BUT:** `actor_identity` uses `on_delete=models.SET_NULL` (already correct)
- Add `outcome` field: ('success', 'denied', 'error')
- **Migration:** `makemigrations omop_core`

#### 5. Invitation acceptance endpoint
- **File:** `patient_portal/api/views.py`
- `POST /api/v1/invitations/accept/` with `{token}`
- Validates token, checks expiration, sets `verification_status='VERIFIED'`
- If token belongs to unregistered email, requires account creation first

#### 6. `MyAccessViewSet` (separate endpoints)
- **File:** `patient_portal/api/views.py`
- `GET /api/v1/my-access/` → metadata only
- `GET /api/v1/my-access/representatives/` → list
- `POST /api/v1/my-access/representatives/` → invite (rate limited: 10/day, 3 pending)
- `PATCH /api/v1/my-access/representatives/<id>/` → revoke (soft delete)
- `GET /api/v1/my-access/providers/` → list
- `POST /api/v1/my-access/providers/` → request (by email/NPI/org)
- `PATCH /api/v1/my-access/providers/<id>/` → revoke
- `GET /api/v1/my-access/professional/` → read-only org access list
- `GET /api/v1/my-access/audit/` → audit log (paginated, date filterable)

#### 7. `PatientConsentViewSet`
- **File:** `patient_portal/api/views.py`
- `GET /api/v1/my-consents/` → list
- `POST /api/v1/my-consents/` → upsert (create or update)
- `DELETE /api/v1/my-consents/<id>/` → withdraw (set `consent_granted=False`)

#### 8. Enforce `PatientConsent` in access control (feature-flagged)
- **File:** `omop_core/authorization.py`
- Add `settings.PHR_CONSENT_ENFORCED` check
- When enabled:
  - After self-access, before representative check: verify `data_sharing` consent
  - For `PersonalRepresentative` access: require `data_sharing` consent
  - For `GroupAccess` with `granted_by_patient=True`: require `data_sharing` consent
  - For org-initiated `GroupAccess`: do NOT require consent (but patient can revoke via `PatientProviderGrant`)
- Update `get_actor_role()` to return `'self'`, `'representative'`, `'provider'`, `'org_admin'`, `'doctor'`, `'analyst'`, or `None`

#### 9. `AuditLogMiddleware` rewrite
- **File:** `patient_portal/api/middleware.py`
- Log writes (POST/PUT/PATCH/DELETE) synchronously to `AccessAuditLog`
- Log reads (GET) to `AccessAuditLog` ONLY for:
  - `/api/v1/patient-records/<id>/` (detail view)
  - `/api/fhir/*` (FHIR operations)
  - `/api/v1/my-data/export/` (export)
- Log denied access (403) with `outcome='denied'`
- Never raises — wraps DB write in try/except
- Continue stdout logging for operational observability

#### 10. Login/logout signal handlers
- **File:** `patient_portal/signals.py` (new)
- `user_logged_in` → `AccessAuditLog.objects.create(action='login', ...)`
- `user_logged_out` → `AccessAuditLog.objects.create(action='logout', ...)`
- Register in `patient_portal/apps.py` `ready()`

#### 11. Access denial logging
- **File:** `omop_core/authorization.py`
- Add `log_access_denied(actor_identity, target_person_id, reason)` helper
- Called from `can_access_patient()` when returning False
- Creates `AccessAuditLog` with `action='access_denied'`, `outcome='denied'`

### Phase 1B: Frontend Patient Portal

**PR: `feature/phr-phase1-frontend`**

Depends on Phase 1A APIs being deployed.

#### 12. New `/my-data` routes and components
- **File:** `frontend/src/App.tsx`
- `/my-data` → shell with sidebar nav
- `/my-data/access` → AccessManagementTab
- `/my-data/activity` → ActivityLogTab
- `/my-data/consents` → ConsentManagerTab
- `/my-data/export` → DataExportTab

#### 13. API client extensions
- **File:** `frontend/src/api/axios.ts`
- `myAccess`, `myConsents`, `myData` API methods

#### 14. Type extensions
- **File:** `frontend/src/types/patient.ts`
- `AccessGrant`, `AuditEntry`, `PatientConsentState` interfaces

### Phase 1C: Data Export (Deferred)

**PR: `feature/phr-phase1-export`**

#### 15. Patient data export endpoint
- `GET /api/v1/my-data/export/?format=fhir`
- New module `patient_portal/api/export.py`
- Query OMOP tables, build FHIR Bundle
- Add `ProvenanceRecord` for export event

### Phase 1D: Version History (Deferred)

**PR: `feature/phr-phase1-history`**

#### 16. Add `django-simple-history`
- **File:** `requirements.txt`, `ctomop/settings.py`
- Track history on `PatientRecord` only (defer clinical tables)

---

## Critical Files (Revised)

| File | What to change |
|---|---|
| `omop_core/models.py` | Extend `PersonalRepresentative`; add `PatientProviderGrant`, `AccessAuditLog`; add `granted_by_patient` to `GroupAccess` |
| `omop_core/authorization.py` | Enforce `PatientConsent` (feature-flagged); add `log_access_denied`; update `get_actor_role()` |
| `patient_portal/api/views.py` | Add `MyAccessViewSet`, `PatientConsentViewSet`, invitation acceptance |
| `patient_portal/api/export.py` | **New file:** FHIR export logic |
| `patient_portal/api/middleware.py` | Rewrite `AuditLogMiddleware` (selective read logging) |
| `patient_portal/signals.py` | **New file:** login/logout signal handlers |
| `patient_portal/apps.py` | Register signal handlers in `ready()` |
| `patient_portal/api/v1_urls.py` | Register new viewsets |
| `ctomop/settings.py` | Add `PHR_CONSENT_ENFORCED`, `simple_history` |
| `requirements.txt` | Add `django-simple-history` |
| `frontend/src/App.tsx` | Add `/my-data/*` routes |
| `frontend/src/api/axios.ts` | Add `myAccess`, `myConsents`, `myData` API methods |
| `frontend/src/types/patient.ts` | Add `AccessGrant`, `AuditEntry`, `PatientConsentState` |
| `frontend/src/components/MyData/*.tsx` | **New files:** patient portal components |

---

## Rollback Safety

- All changes are additive (new models, new endpoints, new routes)
- `GroupAccess` and existing provider workflows are untouched
- `PHR_CONSENT_ENFORCED` defaults to `False` — consent is recorded but not enforced until explicitly enabled
- New frontend routes are behind auth; no public surface changes
- Migrations are standard Django; `start.sh` auto-applies on Render deploy
- `PatientProviderGrant` does not affect existing `GroupAccess` logic until a grant is activated

---

## PHR Functions Unlocked (Revised — More Honest)

| Function | After Phase 1A | After Phase 1B | After Phase 1C |
|---|---|---|---|
| PH.0 Personal Health | PARTIAL | PARTIAL | CONFORMANT |
| PH.1.5 Manage Consents | CONFORMANT | CONFORMANT | CONFORMANT |
| PH.3.5.3 Registry of Actors | CONFORMANT | CONFORMANT | CONFORMANT |
| PH.3.5.4 Manage Reminders | NON-CONFORMANT | NON-CONFORMANT | NON-CONFORMANT |
| PH.6.3 Provider-Patient Communications | PARTIAL | PARTIAL | CONFORMANT |
| TI.1.4 Patient Access Management | PARTIAL | CONFORMANT | CONFORMANT |
| TI.1.8 Patient Privacy & Confidentiality | PARTIAL | PARTIAL | CONFORMANT |
| TI.2 Audit | NON-CONFORMANT | PARTIAL | CONFORMANT |
| TI.2.1.1 Record Entry Audit Triggers | PARTIAL | CONFORMANT | CONFORMANT |
| TI.2.1.2.6 Successful Access | NON-CONFORMANT | CONFORMANT | CONFORMANT |
| TI.2.1.2.7 Access Denied | NON-CONFORMANT | CONFORMANT | CONFORMANT |
| RI.1.1.5.1 Evidence of View/Access | NON-CONFORMANT | CONFORMANT | CONFORMANT |
| S.3.3.1 Manage Consents | CONFORMANT | CONFORMANT | CONFORMANT |
| S.3.3.3 Manage Documents for Personal Representation | PARTIAL | PARTIAL | CONFORMANT |
| S.3.5 Manage PHR Output | NON-CONFORMANT | NON-CONFORMANT | CONFORMANT |
| S.3.6 Manage PHR Data Import/Export | NON-CONFORMANT | NON-CONFORMANT | CONFORMANT |

**Key insight:** The original plan claimed too many functions would become CONFORMANT after Phase 1 alone. Audit and export require the full Phase 1C. Reminders are out of scope entirely.

---

## Testing Strategy

### Backend

```bash
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput
```

Test classes to add:
- `PersonalRepresentativeInvitationTest` — invite, accept, revoke, expiration
- `PatientProviderGrantTest` — request, activate, revoke flow
- `MyAccessAPITest` — CRUD on representatives and provider grants
- `PatientConsentEnforcementTest` — verify consent gates access correctly (with PHR_CONSENT_ENFORCED=True)
- `AuditLogPersistenceTest` — verify detail-view reads are logged, list views are NOT
- `LoginLogoutAuditTest` — verify signal handlers create audit rows
- `AccessDenialLoggingTest` — verify denied access creates audit row
- `RateLimitTest` — verify invitation rate limiting

### Frontend

```bash
cd frontend && npm test -- --run
```

Test files to add:
- `frontend/src/components/MyData/AccessManagementTab.test.tsx`
- `frontend/src/components/MyData/ConsentManagerTab.test.tsx`
- `frontend/src/components/MyData/ActivityLogTab.test.tsx`

### Manual Verification Checklist

1. Log in as patient → navigate to `/my-data`
2. Invite representative by email → verify `PENDING` row created
3. Accept invitation → verify `VERIFIED`, access works
4. Log in as representative → verify access to patient's record
5. As patient, revoke representative → verify `revoked_at` set, access denied
6. As patient, view Activity Log → verify events logged
7. Toggle `data_sharing` consent off (with feature flag ON) → verify representative blocked
8. Export data → verify FHIR Bundle download
9. Log in as provider with org `GroupAccess` → verify clinical access still works
10. As patient, revoke provider access → verify 24h grace period

---

## Open Questions

1. **Email infrastructure:** Do we have SMTP configured on Render? If not, invitations return token in response for manual sharing during dev.
2. **NPI validation:** Should we validate NPI format (10 digits, checksum) in `PatientProviderGrant`?
3. **Provider search:** Should patients search for providers by name/NPI, or only invite by email?
4. **Audit log retention:** How long to keep `AccessAuditLog` rows? (Suggest 7 years for HIPAA, with auto-archive after 1 year)
5. **Emergency access:** Do we need a formal emergency override workflow, or is the 24h revocation grace period sufficient?
