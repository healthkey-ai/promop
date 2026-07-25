# PHR-S FM Roadmap — Patient role first

Aligning promop with the HL7 **Personal Health Record System Functional Model R2**
(PHR-S FM, <https://build.fhir.org/ig/HL7/phrsfm-ig/en/>).

## Context

promop is an oncology PHR built on OMOP CDM + FHIR R4, with a Django/DRF backend and a
React SPA. The PHR-S FM is organized entirely around the **PHR Account Holder** — i.e.
the patient — and that is exactly where promop's gap is: today it is a **provider
console**.

- Every authorization role is provider-side: `org_admin`, `doctor`, `analyst`
  (`omop_core/models.py:245`, `:120`).
- The React app has only binary logged-in/out gating over provider routes
  (`frontend/src/App.tsx:49-84`).
- `UserSerializer` exposes no patient linkage (`patient_portal/api/serializers.py:17-49`).

The backend primitive already exists but is second-class: `PatientUser` is a
`OneToOne(Identity) ↔ OneToOne(Person)` link (`patient_portal/models.py:97-115`), and
`PatientRecordViewSet.get_queryset` already scopes a plain authenticated user to their
own `person_id` (`patient_portal/api/views.py:198-203`). That self-scoping is implicit
and **not enforced on the other OMOP viewsets** (Conditions, Measurements, etc., which
filter by a client-supplied `person_id` via `_OmopFilterMixin`).

**Scope:** a pragmatic, oncology-focused subset of the FM — Patient role foundation
first, then the highest-value account-holder functions. **Not** full-FM conformance
across the Supportive / Record-Infrastructure / Trust-Infrastructure sections.

**UI:** add a **patient mode to the existing React SPA** (role-gated routes), reusing
`useAuth`, `axios`, and the `PatientInfo` tab components.

**Delivery rule (per CLAUDE.md):** each phase ships as its own feature branch + PR into
`dev` with backend + frontend tests, code review, and the full backend suite run after
merge. Each phase below is independently shippable.

---

## Phase 1 — First-class Patient / PHR Account Holder role  ✅ DONE

**Status:** Delivered in PR #265 (issue #264), merged to `dev` 2026-07-24; migration
applied on staging. Shipped: the patient role surface (`patient_person_for`,
`is_patient`/`person_id` on `/api/v1/user/`), patient-mode React SPA (role-gated routing +
`PatientHome`), and three account-provisioning paths — staff email invite/accept
(`/api/v1/patients/{id}/invite/`, `/patient-invitations/accept/`) and app-driven signup
(`/api/v1/patients/signup/`). Per-patient access enforcement was already present
(`can_access_patient` + `_OmopFilterMixin`) and is covered by existing tests. 810 backend /
68 frontend tests green. The design detail below is retained as the as-built record.

**Post-merge hardening (same PR series):**
- **`PatientSelfScopePermission`** — centralized object-level permission
  (`permissions.py`) applied to all OMOP viewsets as belt-and-suspenders on top of
  queryset filtering. Uses `_resolve_person_id()` to extract `person_id` from any OMOP
  model (including `EpisodeEvent` via its bare `episode_id` → `Episode` lookup). Bypasses
  for service tokens, staff, superusers, and non-patient identities.
- **Patient account deletion (GDPR right to erasure)** — `DELETE /api/v1/patient-records/me/`
  with `{"confirm": "DELETE"}`. Hard-deletes all patient data (Person cascade + orphan
  EpisodeEvent cleanup + Identity removal) inside `transaction.atomic()`. Frontend: profile
  dropdown in `PatientDetail` header replaces standalone "Sign out" button; includes
  `DeleteAccountDialog` with typed confirmation.
- **FM:** TI.1.7 (Account Holder data deletion).

**FM:** PH.1 (PHR Account Holder Profile), TI.1 (Security / access control).

**Goal:** a patient can log in and **view/edit their own record** (and only their own),
enforced uniformly, with both API and frontend explicitly aware that "this user is a
patient." Viewing the own record is delivered here — `PatientHome` is Phase 1's UI.
Phase 2 adds *export* of that record, not the ability to see it.

### Backend

1. **Define the role by the `PatientUser` link — no schema churn.** Add a helper to
   `patient_portal/services.py` (next to `resolve_or_create_person` `:15-73`):
   `patient_person_for(identity) -> Person | None` = the linked `PatientUser.person`
   when the identity has a `PatientUser` and **no provider `GroupAccess`** grant. This is
   the canonical "is this a patient identity" test.
2. **Hard self-scoping guard.** Add a permission/mixin to
   `patient_portal/api/permissions.py` (alongside `ScopedTokenPermission` `:43-96`, which
   is documented *not* to enforce object ownership at `:57-67`):
   `PatientSelfScopePermission` + a queryset helper that, when
   `patient_person_for(request.user)` is set, forces every OMOP viewset to filter to that
   single `person_id` and rejects object access to any other Person (403). Apply it to the
   `_OmopFilterMixin` viewsets: `ConditionOccurrenceViewSet`, `DrugExposureViewSet`,
   `MeasurementViewSet`, `ObservationViewSet`, `ProcedureOccurrenceViewSet`,
   `EpisodeViewSet`, `PatientDocumentViewSet`, `PatientTrialEnrollmentViewSet`,
   `PatientSurveyResponseViewSet`. Refactor `PatientRecordViewSet.get_queryset`
   (`api/views.py:174-238`) to call the same helper so the logic lives in one place.
3. **SMART `launch/patient` → Person.** Today OAuth2 tokens scope by org only and
   `launch/patient` is unhandled. When a token carries `launch/patient` / `patient/*`
   scope, resolve it to the token owner's `PatientUser.person` via the same helper (touch
   `patient_portal/api/authentication.py` / `permissions.py`). Provider org-scoping
   unchanged.
4. **Surface the role.** Extend `UserSerializer` (`api/serializers.py:17-49`) with
   `is_patient: bool` and `person_id`, computed live like the existing `is_org_admin`.
   `CurrentUserViewSet` (`api/views.py:112-123`) needs no change.

### Frontend

5. `frontend/src/hooks/useAuth.ts` — add `is_patient` and `person_id` to the `User`
   interface.
6. `frontend/src/App.tsx:49-84` — replace binary gating with **role-gated routing**:
   `is_patient` → `PatientHome`; otherwise the existing provider console. Provider routes
   stay off-limits to patients.
7. New `frontend/src/components/Patient/PatientHome.tsx` — loads the patient's own record
   (`GET /api/v1/patient-records/` returns just theirs under the new scope) and renders it
   with the existing `PatientInfo` tabs (`GeneralTab`, `LabsTab`, disease tabs) in
   read/limited-edit mode. Reuse `PatientDetail.tsx` where practical.
8. `frontend/src/components/Auth/Login.tsx` (currently titled "PROMOP Admin") — make copy
   role-neutral; no separate login endpoint needed (`login_view` already accepts
   non-provider identities, `api/views.py:3291-3323`).

### Account provisioning (how a patient gets an account)

Three sanctioned paths, all landing on the same `Identity → PatientUser → Person`
link — the org is always known (from the caller's token or the record):

1. **Staff invite** — `POST /api/v1/patients/{id}/invite/` emails the patient a
   tokenised link; they set a password at `/accept-patient-invite`
   (`patient_portal/api/patient_invitations.py`). Org derives from
   `person → PatientRecord.organization`.
2. **App-driven signup ("A")** — `POST /api/v1/patients/signup/`, for a trusted app
   (org-scoped OAuth client, service token, or staff). Creates the account and stamps
   the org (from the caller's token, or an explicit `org` slug for staff/service).
   Accepts either OIDC keys (`actor_iss`/`actor_sub`, so the patient's later JWT login
   matches) or `email`+`password` (`patient_portal/api/patient_signup.py`).
3. **Legacy auto-provision** — partner-auth (patient JWT) still auto-creates on first
   call (`authentication.py` → `resolve_or_create_person`), but assigns **no org**;
   prefer (1) or (2) so the org is set up front.

**Not required for PHR-S FM conformance:** the FM (PH.1 / TI.1) is agnostic about
provisioning *mechanics* — it requires only that an account holder can be established,
authenticated, and manage their record, which (1)+(2) satisfy. So "patient-JWT
auto-provision with org" (model B) is not a conformance gap.

**Deferred — direct patient self-signup** (a patient registers themselves, e.g. an
org-gated `/org/{slug}/signup` UI): a reasonable optional capability, its own later PR.
It needs an `Organization.allows_public_signup` flag (default off), email verification,
and a duplicate/claim guard, so it is kept out of this phase.

### Tests

- Backend (`patient_portal/tests.py`): patient identity sees only own PatientRecord;
  cross-person access on the OMOP viewsets → 403/empty (pre-existing coverage);
  `UserSerializer` returns `is_patient`/`person_id`; `patient_person_for` role logic;
  invitation create/lookup/accept + email-editable; app-driven signup (org stamping,
  OIDC + local, idempotency, permission gating).
- Frontend: `App.test.tsx` (role-gated routing), `PatientHome.test.tsx`,
  `AcceptPatientInvite.test.tsx`.

---

## Phase 2 — Own-record FHIR export  ✅ DONE

**FM:** PH.2 (Manage Historical & Current-State Data), PH.2.4 (Ad-hoc views), S.3
(import/export).

**Status:** Implemented. Export service (`omop_core/services/fhir_export.py`), API endpoint
(`GET /api/v1/patient-records/{person_id}/export-fhir/`), management command
(`export_fhir_bundle`), and frontend "Download my record (FHIR)" button on `PatientHome`.
830 backend / 75 frontend tests green.

*(Own-record viewing lands in Phase 1 via `PatientHome`. This phase adds the ability to
export that record as FHIR, plus any read-only presentation refinements.)*

**Detailed sub-plan:** see [`docs/phase2-fhir-export-plan.md`](phase2-fhir-export-plan.md).

**Gap:** promop has three FHIR **import** paths but **no export** of a real patient's data
(`generate_fhir_bundle` is synthetic-only; `export_org_patients` emits raw JSON, not FHIR).

- Add an **OMOP→FHIR R4 `$everything`-style export**:
  `GET /api/v1/patient-records/{id}/export-fhir/` (`@action` on `PatientRecordViewSet`) +
  a mirror management command for large exports (pattern of `import_fhir_bundle.py`'s
  in-process `RequestFactory` driver to dodge Render's 30s limit). Serialize the patient's
  OMOP rows (Condition/Drug/Procedure/Measurement/Observation/Visit/Immunization) into a
  FHIR `Bundle` — invert the mapping already in `upload_fhir` (`api/views.py:682`+) so
  import/export round-trip.
- Frontend: "Download my record (FHIR)" action on `PatientHome`. Optionally gate full
  export behind the existing `is_premium` flag (`patient_portal/models.py:59`).
- Tests: export produces a valid Bundle; import→export round-trip preserves core resources;
  patient can export only their own record (reuses the Phase-1 scope guard).

---

## Phase 3 — Consent grants  ✅ DONE

**FM:** PH.1.5 (Manage Consents and Authorizations).

**Status:** Delivered in PR #283 (issue #278), merged to `dev` 2026-07-25. Shipped:
`PatientConsentViewSet` at `/api/v1/consents/` (list + PATCH toggle), auto-creates all 3
consent types on first list, queryset self-scoped to the authenticated patient.
`_resolve_person_id` extended with `patient_user_id` pattern for `PatientSelfScopePermission`.
Frontend `PatientConsents` component with toggle switches on `PatientHome`. `consent_date`
changed to `auto_now=True` so timestamp reflects actual consent decision time.
863 backend / 81 frontend tests green.

**Existing:** `PatientConsent` (`patient_portal/models.py:173`, keyed to `PatientUser`,
boolean-per-type), `consent_management` server view (`views.py:113`), and
`FhirPatientConsentView` (`api/fhir/sync.py:791`).

*Stretch (deferred):* represent grants as FHIR `Consent` resources. The current
boolean-per-type model is adequate for the oncology use case.

---

## Phase 4a — Patient-originated data (surveys in patient mode)  ✅ DONE

**FM:** PH.2.1 (Account-Holder-Originated Data), PH.3.1.

**Status:** Delivered in PR #285 (issue #284), merged to `dev` 2026-07-25. Shipped:
`SurveyResponsePermission` allowing session-auth patients to POST (start surveys),
`PatientSurveys` list component with status badges and start/continue/view actions,
`SurveyForm` multi-page renderer with autosave on page navigation, progress tracking,
and read-only completed view. Review hardening: `completed_at` validation (no re-open),
immutable person/survey on PATCH, permission method whitelist, cross-patient POST test.
869 backend / 103 frontend tests green.

**Existing:** Surveys/PRO capture is solid (`Survey` / `PatientSurveyResponse`,
`omop_core/models.py:2473,2510`; viewsets `api/views.py:4124,4199`); HealthKit device sync
(`api/fhir/sync.py`).

---

## Phase 4b — Bidirectional messaging ✅ DONE

**FM:** PH.6 (Manage Encounters w/ Providers).

**As-built (PR #289):**

- Added `parent` (self-FK for threading), `sender` (Identity FK), `read_at` fields to `PatientMessage`
- `PatientMessageViewSet` under `/api/v1/messages/` with threading, read-state, and self-scoping
- `perform_create` auto-sets sender/patient_user; cross-patient reply guard; sender-only edits
- `MessagePagination` (page_size=50, `-created_at` ordering)
- N+1 prevention via `Count('replies')` annotation
- `mark-read` custom action
- Frontend `PatientMessages.tsx`: thread list, conversation view, compose, chat-bubble UI
- 11 backend tests (isolation, threading, cross-patient blocks, filters)
- 8 frontend tests

---

## Phase 5 — Account-holder clinical lists (oncology-relevant subset) ✅ DONE

**FM:** PH.1.4 (Advance Directives), PH.2.5 (problem/med/allergy/immunization lists),
PH.3 (care plans).

**As-built:**

- **Advance Directives** — added `ADVANCE_DIRECTIVE` to `PatientDocument.DOC_TYPE_CHOICES`;
  `PatientDocumentViewSet` extended with `doc_type` query param filtering; frontend
  `AdvanceDirectives.tsx` component (list + upload) wired into `PatientHome`.
- **Immunization List** — immunization `DrugExposure` rows tagged with
  `route_source_value='VACCINE'` in both FHIR sync (`sync.py`) and legacy upload
  (`views.py`). Read-only `ImmunizationListViewSet` at `/api/v1/immunizations/` with
  `ImmunizationSerializer` (vaccine_name, date, lot_number). Frontend
  `ImmunizationList.tsx` table on `PatientHome`.
- **Allergy List** — allergy `Observation` rows tagged with `qualifier_source_value='ALLERGY'`
  in FHIR sync; `AllergyIntolerance` FHIR resource collection + write added to legacy
  upload handler. Read-only `AllergyListViewSet` at `/api/v1/allergies/` with
  `AllergySerializer` (allergen_name, criticality, clinical_status, recorded_date).
  Frontend `AllergyList.tsx` table on `PatientHome`.
- Care plans/goals deferred — no concrete oncology driver.
- 9 new backend tests (AD creation/filtering, immunization list + exclusion, allergy list
  + exclusion + FHIR upload integration). 112 frontend tests green.

---

## Cross-cutting

- **FM traceability:** maintain a short mapping of each shipped capability to its FM
  function ID (PH.1.x, etc.) so scope stays visible and a future conformance pass has a
  starting point.
- **Audit (TI.2):** the FM expects consent/record-access audit trails. A follow-up, not in
  the pragmatic subset unless a phase surfaces a concrete need.

## Verification (per phase)

```bash
# Backend suite (local PostgreSQL, matches CI)
DATABASE_URL="postgresql://postgres@localhost:5432/promop_test" \
  .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput
# Frontend
cd frontend && npm test -- --run
```

Manual end-to-end for Phase 1: create an `Identity` + linked `PatientUser` for a test
Person, log in through the SPA, confirm it lands on `PatientHome` with only that record,
and confirm direct API calls for another `person_id` return 403/empty on every OMOP
viewset. Run the DB/model sync check (CLAUDE.md) after any migration.
