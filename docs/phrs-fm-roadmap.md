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

## Phase 1 — First-class Patient / PHR Account Holder role  ⟵ do first

**FM:** PH.1 (PHR Account Holder Profile), TI.1 (Security / access control).

**Goal:** a patient can log in and see/edit **only their own** record, enforced
uniformly, and both API and frontend explicitly know "this user is a patient."

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

### Tests

- Backend (`patient_portal/tests.py`, `_SmartBase`/OAuth base): patient identity sees only
  own PatientRecord; cross-person GET/PATCH on **every** OMOP viewset → 403/empty; provider
  scoping unchanged; `UserSerializer` returns `is_patient`/`person_id`; SMART
  `launch/patient` token resolves to the right Person.
- Frontend (`PatientHome.test.tsx`, `App.test.tsx`): patient user routes to `PatientHome`;
  provider routes redirect for patients; `useAuth` exposes new fields.

---

## Phase 2 — View own record + FHIR export

**FM:** PH.2 (Manage Historical & Current-State Data), PH.2.4 (Ad-hoc views), S.3
(import/export).

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

## Phase 3 — Consent grants

**FM:** PH.1.5 (Manage Consents and Authorizations).

**Existing:** `PatientConsent` (`patient_portal/models.py:118`, keyed to `PatientUser`,
boolean-per-type), `consent_management` server view (`views.py:114`), and
`FhirPatientConsentView` (`api/fhir/sync.py:790`).

- Promote consent to a first-class DRF resource under `/api/v1/`: list/grant/revoke the
  patient's own consents (self-scoped via the Phase-1 guard), with `consent_type`, granted
  flag, timestamp, and optional scope note. Reuse the existing `PatientConsent` model;
  types data_sharing / clinical_trial / research already seeded.
- Frontend: a Consents view in patient mode (reuse `FormField`/`Select` primitives).
- Tests: grant/revoke, self-scope enforcement, uniqueness per type.

*Stretch (deferred):* represent grants as FHIR `Consent` resources. The current
boolean-per-type model is adequate for the oncology use case.

---

## Phase 4 — Patient-originated data & messaging

**FM:** PH.6 (Manage Encounters w/ Providers), PH.2.1 (Account-Holder-Originated Data),
PH.3.1.

**Existing:** Surveys/PRO capture is solid (`Survey` / `PatientSurveyResponse`,
`omop_core/models.py:2473,2510`; viewsets `api/views.py:4124,4199`); HealthKit device sync
(`api/fhir/sync.py`); messaging is **one-way, server-rendered, no provider recipient**
(`PatientMessage` `patient_portal/models.py:138`, `views.py:80`).

- Expose surveys + responses in patient mode (self-scoped) so patients complete PROs from
  the SPA.
- Upgrade messaging to bidirectional: add a DRF endpoint under `/api/v1/`, a
  recipient/thread concept, and read-state, replacing the template-only flow. Render in
  patient mode.
- Tests: patient completes a survey (own responses only); message create/list/reply
  self-scoped.

---

## Phase 5 — Account-holder clinical lists (oncology-relevant subset)

**FM:** PH.1.4 (Advance Directives), PH.2.5 (problem/med/allergy/immunization lists),
PH.3 (care plans).

**Gap:** allergies and immunizations are folded into generic OMOP Observation/DrugExposure
rows with no structured list; care plans, advance directives, and goals are absent.

- Prioritize by oncology value; likely: structured **allergy list** and **immunization
  list** as read models derived from OMOP (mirroring how `PatientRecord` is derived),
  surfaced as `PatientInfo` tabs. Add **advance directives** as a document type (extend
  `PatientDocument.doc_type`, `omop_core/models.py:2369`).
- Defer care plans/goals unless a concrete oncology driver appears.
- Tests per new list/field following the CLAUDE.md "new attribute → all layers" rule.

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
