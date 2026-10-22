# PHR-S FM Traceability Matrix

Maps HL7 **Personal Health Record System Functional Model R2**
(PHR-S FM, <https://build.fhir.org/ig/HL7/phrsfm-ig/en/>) functions to promop's
as-built capabilities. This is the conformance-posture map referenced as the
"FM traceability" cross-cutting item in [`phrs-fm-architecture.md`](phrs-fm-architecture.md);
update it whenever a capability lands so a future formal conformance pass has a
starting point.

> **Update 2026-07-27 — WS0 complete + re-verified, incl. #318.** The remediation workstream
> #301–#308 merged, a criterion-level re-verification was run, and the audit hash chain (#318/#320)
> then closed the last Essential residual: **75 MET / 3 PARTIAL / 0 NOT MET** across the 78 audited
> SHALL. Now **conformant**: PH.1.1, PH.1.2, PH.1.4, PH.2.3, PH.6.3, TI.1.1, TI.1.2, TI.1.7, TI.2.2,
> **TI.2.2.1** (hash chain), TI.2.3, TI.4.2, TI.5.1.1, S.3.6 (plus the already-conformant set) — the
> profile's **entire Essential set now conforms**. **Still partial (both Optional):**
> **TI.5.2** (multi-version); **TI.5.4** (agreement enforcement). The
> **authoritative** per-function status is the self-attestation
> [`phrs-fm-conformance-claim.md`](phrs-fm-conformance-claim.md) and
> [`phrs-fm-onco-profile.md`](phrs-fm-onco-profile.md); the per-row ◐/○ statuses in the tables
> below predate WS0.

**Status legend**

| | Meaning |
|---|---|
| ✅ | Implemented — a concrete promop capability satisfies the function |
| ◐ | Partial — some of the function is covered; gaps noted |
| ○ | Not yet / deferred — no capability, or intentionally out of the pragmatic subset |

Scope note: promop targets a **pragmatic, oncology-focused subset** of the FM
(patient/account-holder-facing functions), not full-FM conformance. Sections with
no account-holder-facing driver (much of Supportive / Record-Infrastructure) are
intentionally ○.

---

## PH — Personal Health

| FM ID | Function | Status | promop capability | Ref |
|---|---|---|---|---|
| **PH.1** | **PHR Account Holder Profile** | ✅ | First-class patient role | #264 |
| PH.1.1 | Identify & maintain account-holder record | ✅ | `Identity` + `PatientUser`↔`Person`; provisioning via staff invite (`/api/v1/patients/{id}/invite/` + `/patient-invitations/accept/`) and app signup (`/api/v1/patients/signup/`); `patient_person_for()` | #264 |
| PH.1.2 | Manage demographic information | ✅ | `Person` demographic/profile API + `PatientHome` General tab; clinical facts are written to OMOP and re-derived into read-only PatientRecord | #264/#489 |
| PH.1.3 | Manage account-holder & family preferences | ○ | No dedicated preferences store | — |
| PH.1.4 | Manage advance directives | ✅ | `PatientDocument` `ADVANCE_DIRECTIVE` type + `doc_type` filter; `AdvanceDirectives.tsx` | #292 |
| PH.1.5 | Manage consents & authorizations | ✅ | `PatientConsentViewSet` `/api/v1/consents/` (list + toggle), self-scoped; 3 consent types | #278/#283 |
| PH.1.6 | Manage account status | ◐ | Account deletion / right-to-erasure done (see TI.1.7); no suspend/reactivate lifecycle states | #264 |
| **PH.2** | **Manage historical & current-state data** | ✅ | | #2xx |
| PH.2.1 | Account-holder-originated data | ✅ | Surveys/PROs in patient mode (`/api/v1/survey-responses/`); HealthKit device sync (`fhir/sync.py`) | 4a |
| PH.2.2 | Data from external administrative sources | ◐ | FHIR import ingests admin/demographic data; no dedicated admin-source workflow | — |
| PH.2.3 | Data/documentation from external clinical sources | ✅ | Three FHIR R4 import paths (`upload_fhir`, `import_fhir_bundle`, `fhir/sync.py`) | — |
| PH.2.4 | Produce & present ad-hoc views | ✅ | `PatientHome` tabbed views; **FHIR export** `GET /api/v1/patient-records/{id}/export-fhir/` + `export_fhir_bundle` command | Phase 2 |
| PH.2.5 | Manage historical/current-state lists | ◐ | Problems (`ConditionOccurrence`), meds (`DrugExposure`/LoT), **allergy list** (`/api/v1/allergies/`), **immunization list** (`/api/v1/immunizations/`); family/genetic/social history partial | #292 |
| **PH.3** | **Wellness, preventive medicine & self-care** | ◐ | | — |
| PH.3.1 | Personal clinical measurements & observations | ✅ | HealthKit/device `Measurement` sync + wearable summaries | — |
| PH.3.4 | Manage medications | ◐ | `DrugExposure` + line-of-therapy fields; no patient-managed active-med reconciliation | — |
| PH.3.2/3.3 | Care plans (account- & provider-initiated) | ○ | Deferred — no concrete oncology driver | — |
| **PH.4** | Manage health education | ○ | Not started | — |
| **PH.5** | Account-holder decision support | ○ | Not started (candidate: drug-interaction / guideline alerts) | — |
| **PH.6** | **Manage encounters with providers** | ◐ | | — |
| PH.6.3 | Provider ↔ account-holder communications | ✅ | Bidirectional secure messaging `/api/v1/messages/` (threads, read-state) | #287/#289 |
| PH.6.1–6.8 | Administrative/financial, referrals, care plans, assessments | ○ | Not started | — |

---

## S — Supportive

| FM ID | Function | Status | promop capability | Ref |
|---|---|---|---|---|
| S.1 | Provider information | ○ | Not started | — |
| S.2 | Financial management | ○ | Not started (out of oncology scope) | — |
| S.3 | Administration management | ◐ | | — |
| S.3.6 | Information import/export | ✅ | FHIR R4 import (3 paths) + FHIR export (Phase 2); raw OMOP JSON export command | — |
| S.4.1 | Manage clinical research information | ◐ | `PatientTrialEnrollment` + EXACT trial-matching (backend); **patient-facing trial surfacing pending** (candidate next area) | — |

---

## RI — Record Infrastructure

| FM ID | Function | Status | promop capability | Ref |
|---|---|---|---|---|
| RI.1 | Record lifecycle & lifespan | ◐ | `ProvenanceRecord` captures source/actor/reason on writes; `previous_values` returned on PATCH; no full lifecycle-event ledger | — |
| RI.2 | Record synchronization | ◐ | FHIR sync/patient-sync endpoints ingest & reconcile external EHR/device data | — |
| RI.3 | Record archive & restore | ○ | Not started (audit-log archival addressed separately under TI.2.2, #298) | — |

---

## TI — Trust Infrastructure

| FM ID | Function | Status | promop capability | Ref |
|---|---|---|---|---|
| **TI.1** | **Security** | ✅ | | — |
| TI.1.1 | Authentication | ✅ | OAuth2 / SMART-on-FHIR (PKCE), partner JWT (OIDC/Firebase), session, HMAC service token | — |
| TI.1.2 | Authorization / access control | ✅ | `ScopedTokenPermission` (SMART scopes), `GroupAccess` roles, `can_access_patient`/`can_write_patient`, `PatientSelfScopePermission` (per-Person object scoping), org/trust scoping | #264 |
| TI.1.7 | Account-holder data deletion (erasure) | ✅ | `DELETE /api/v1/patient-records/me/` (typed confirm; Person cascade + Identity removal, atomic) | #264 |
| TI.1.x | Privacy / data masking / routing | ◐ | Org-scoping, trust maps, `hide_from_patient` groundwork; no field-level masking | — |
| **TI.2** | **Audit** | ✅ | | #295 |
| TI.2.1 | Audit triggers | ✅ | `AuditLogMiddleware` audits every API/OAuth request; classified `record_view`/`record_create`/`record_update`/`record_delete`/`auth`/`consent` | #295 |
| TI.2.2 | Audit log management (retention) | ✅ | Immutable `AuditEvent` rows (admin view-only); `prune_audit_events` command + `AUDIT_EVENT_RETENTION_DAYS` setting (default ~6y) with `--dry-run`/`--days`/`--archive` | #298/#299 |
| TI.2.3 | Audit notification & review | ✅ | Read-only `GET /api/v1/audit-events/` — staff see all, patients see own; filter by type/method/user/time | #295 |
| TI.4 | Standard terminology & services | ✅ | OMOP concept model; vocabulary endpoints; concept search/lookup/ancestors/descendants/graph/synonyms | #239 |
| TI.5 | Standards-based interoperability | ✅ | FHIR R4 (import/export), OMOP CDM v5.4, SMART-on-FHIR / OAuth2, `.well-known/smart-configuration` | — |
| TI.3 / TI.6–TI.10 | Registry, business rules, workflow, backup, terminology models | ○ | Not separately implemented (infra-level; partly delegated to platform) | — |

---

## Summary

- **Complete (✅):** the account-holder core — PH.1 profile & provisioning, PH.1.4 directives,
  PH.1.5 consent, PH.2 data management incl. FHIR export, PH.6.3 messaging, TI.1 security,
  TI.2 audit incl. retention (#295/#298), TI.4/TI.5 terminology & interoperability.
- **Partial (◐):** PH.2.5 clinical lists (family/genetic/social history gaps), PH.3 wellness,
  RI.1/RI.2 record infrastructure, S.4.1 research (backend only), TI.1.x privacy masking.
- **Not yet / deferred (○):** PH.4 education, PH.5 decision support, PH.6 referrals, most of
  Supportive (S.1/S.2), RI.3 archive, TI.3/TI.6–TI.10.
- **Highest-value open candidates** (from the roadmap): **S.4.1** patient-facing trial matching
  (big head start from the EXACT integration), **PH.5** oncology decision support, and closing
  the **PH.2.5** history gaps.
