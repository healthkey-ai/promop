# HL7 PHR-S FM R2 — Conformance Claim via Self-Attestation

> **Status:** DRAFT for authorized signature. Prepared 2026-07-26. Self-attestation —
> not validated by the HL7 EHR WG. The §5 functions were verified criterion-by-criterion
> against the source code (see Appendix A); the signatory should confirm the determination
> before issuing.
>
> **Update 2026-07-27 (post-WS0):** the remediation workstream **#301–#308 is complete and
> merged** — every §6 gap has an implemented, tested fix (see the closed-issue table in
> Appendix B and [`phrs-fm-onco-profile.md`](phrs-fm-onco-profile.md), whose ~26 Essential
> functions now all conform). The §4/§5/§6 figures below are the **original point-in-time
> audit** and are intentionally left unchanged; **re-run the criterion-level verification to
> promote the now-fixed §6 functions into §5** before issuing a formal attestation.

## 1. System & vendor identification
| Field | Value |
|---|---|
| Product | **promop** (oncology PHR / patient portal) |
| Vendor / attesting entity | **HealthKey.ai** |
| Product version | `dev` @ `6e86618` (2026-07-26) |
| Attestation type | **Self-Attestation** |
| Attestation date | **2026-07-26** |
| Authorized representative | **Adam Blum** — adam@healthkey.ai |

## 2. Standard & basis
- **Standard:** HL7 Personal Health Record System Functional Model, **Release 2 (PHR-S FM R2)**.
- **Basis:** a **partial, function-level claim** — a vendor-selected, oncology / account-holder-focused
  subset. Not a claim against a published Functional Profile, and not a claim of full-model
  conformance. Per Ch. 5 a function is claimed conformant only when **all its mandatory (SHALL)
  criteria** are met (SHOULD/MAY are optional); per Ch. 7 this claim does not constitute a new
  Functional Profile.

## 3. Attestation statement
> HealthKey.ai attests that promop, as identified in §1, satisfies the mandatory (SHALL)
> conformance criteria of each PHR-S FM R2 function listed in §5, as verified by source-code
> review on the attestation date. Functions evaluated but not fully satisfying their SHALL
> criteria are disclosed in §6 and are **not** claimed.

## 4. Conformance determination method
- **Method:** Self-attestation via **structured source-code verification** — each SHALL criterion
  of every candidate function was mapped to implementing code (or found absent) and graded
  MET / PARTIAL / NOT MET / N/A (Appendix A). **No HL7 conformance-determination tool** was used.
- **Scope evaluated:** 26 candidate leaf functions, **78 SHALL criteria**. No criterion was ruled
  N/A — all conditional dependencies (passwords, patient + provider roles, external ingest) apply.
- **Result:** **49 MET · 18 PARTIAL · 11 NOT MET.**
- **Supporting evidence:** the traceability matrix (`docs/phrs-fm-traceability.md`) and the automated
  regression suite (915 backend + 112 frontend tests passing).

## 5. Functions CLAIMED conformant *(all applicable SHALL met)*
| FM ID | Function | Determination |
|---|---|---|
| PH.1.5 | Manage Consents and Authorizations | 2/2 SHALL MET |
| PH.2 | Manage Historical & Current-State Data | 2/2 MET |
| PH.2.1 | Manage Account-Holder-Originated Data | 3/3 MET |
| PH.2.4 | Produce & Present Ad-hoc Views | no SHALL criteria (all SHOULD/MAY); function implemented |
| PH.3.1.1 | Manage Personal Observations & Care | 1/1 MET |
| TI.4.1 | Standard Terminology & Terminology Models | 3/3 MET |
| TI.4.3 | Terminology Mapping | 1/1 MET |
| TI.5.3 | Standards-Based Application Integration | 1/1 MET |
| TI.5.5 | System Integration | 1/1 MET |
| TI.2 | Audit *(this parent function's own 2 SHALL)* | 2/2 MET — **claimed for TI.2 only; children TI.2.1/2.2/2.3 NOT claimed** |

## 6. Functions evaluated but NOT claimed *(≥1 SHALL unmet — disclosed)*
| FM ID | Primary blocking criterion | Remediation |
|---|---|---|
| PH.1.1 | #06 cannot represent data as *erroneous* while retained (NOT MET) | #307 |
| PH.1.2 | #05 no preference/consent-driven demographic rendering (PARTIAL) | #307 |
| PH.1.4 | #04 advance-directive lacks "in effect" status / effective date (PARTIAL) | #307 |
| PH.2.3 | #09 ingest authenticates channel but not *content* integrity (PARTIAL) | #306 |
| PH.6.3 | #04 proxy-authorization has no render API; #08 no confidentiality tags (PARTIAL) | #308 |
| S.3.6 | #10 no true non-repudiation / crypto signing (PARTIAL) | #306 |
| TI.4.2 | #05 vocab deprecation & #07 embedded-term substitution (NOT MET); version history (PARTIAL) | #305 |
| TI.5.1.1 | #03 depends on full TI.4, capped by TI.4.2 (PARTIAL) | #305 |
| TI.5.2 | #01 single interchange-standard version, FHIR R4 only (NOT MET) | #306 |
| TI.5.4 | #01 no formal interchange-agreement artifact (PARTIAL) | #306 |
| TI.1.1 | #03 lockout, #04/#05 password reuse timeframe/history, #09 force-change (NOT MET); #06 strength bypassed, #08 admin reset (PARTIAL) | #301, #302 |
| TI.1.2 | #04 no field-level revision history (PARTIAL) | #307 |
| TI.1.7 *(Secure Data Routing)* | #02 no audited source/destination status registry (PARTIAL) | #306 |
| TI.2.1 | #01 admin/background security events off the audited path (PARTIAL) | #303 |
| TI.2.2 | #01 non-standard audit format; #04 audit-log access not audited (NOT MET) | #303 |
| TI.2.2.1 | #01 no indelibility / tamper-evidence (PARTIAL) | #304 |
| TI.2.3 | #04 no break-glass emergency-access authorization (NOT MET) | #304 |

## 7. Limitations & caveats
1. **Function-level claim** — verified at SHALL-criterion level for the candidate set; not a claim
   against a named Functional Profile nor of full-model conformance.
2. **Corrections vs. the initial draft:** TI.1.7 is *Secure Data Routing* (not erasure); the
   account-erasure capability (`DELETE …/me/`) is real but was not matched to a conformant function
   here. TI.1/TI.2 do **not** conform at sub-function level despite strong underlying capabilities.
3. **"Essential Now" priority not reconciled** against the normative Function List.
4. **Not HL7-validated** — self-attestation; accuracy is the vendor's responsibility.

## 8. Signatory
| | |
|---|---|
| Name / title | Adam Blum, HealthKey.ai |
| Signature | *__________________________* |
| Date | 2026-07-26 |

---

## Appendix A — Per-criterion determination (78 SHALL)
Verdicts from source-code verification, 2026-07-26. **✅ MET · ◐ PARTIAL · ✗ NOT MET.**

### PH — Personal Health
| Criterion | V | Evidence |
|---|---|---|
| PH.1.1#02 | ✅ | Multiple identifiers: `Person.person_id`, `actor_iss`/`actor_sub`, `FhirConnection.fhir_patient_id` |
| PH.1.1#04 | ✅ | `person_id` PK; `Identity` (issuer,sub)+uid; `PatientUser` 1:1 |
| PH.1.1#06 | ✗ | No entered-in-error/erroneous flag on patient data (only overwrite-with-reason) |
| PH.1.2#01 | ✅ | Discrete demographics on `Person`/`PatientRecord`, writable via create + `me` PATCH |
| PH.1.2#02 | ✅ | Typed columns; discrete DRF serializer fields |
| PH.1.2#03 | ✅ | `upload_fhir` + `FhirSyncView._update_demographics` import demographics |
| PH.1.2#05 | ◐ | Org/row scoping only; no consent/preference-driven rendering or de-identification |
| PH.1.2#07 | ✅ | `patient_name` emitted with retrieve/list/`me` |
| PH.1.4#01 | ✅ | `PatientDocument` ADVANCE_DIRECTIVE + `?doc_type=` filter |
| PH.1.4#04 | ◐ | No "in effect" status / effective date; no organ-donation metadata |
| PH.1.4#07 | ✅ | `uploaded_at` + TI.2 audit of mutations |
| PH.1.5#01 | ✅ | `PatientConsent` + `/api/v1/consents/`; survey consent |
| PH.1.5#04 | ✅ | grant/withhold/revoke via PATCH; `EVENT_CONSENT` audit rows |
| PH.2#01 | ✅ | `ProvenanceRecord` author/source/custodian per write |
| PH.2#02 | ✅ | `AuditEvent` per request; `/api/v1/audit-events/` |
| PH.2.1#01 | ✅ | Unstructured: messages/documents/survey free-text |
| PH.2.1#02 | ✅ | Structured: `me` PATCH, survey `values`, HealthKit `Measurement` |
| PH.2.1#03 | ✅ | `ProvenanceRecord` author/source (UDI not captured) |
| PH.2.3#05 | ✅ | External data surfaced via API / OMOP viewsets / export |
| PH.2.3#08 | ✅ | Structured FHIR ingest → OMOP rows |
| PH.2.3#09 | ◐ | Sender/channel authenticated; no content signature/digest/integrity |
| PH.3.1.1#01 | ✅ | `/api/fhir/patient-sync/` self-ingest + `me` vitals + surveys |
| PH.6.3#01 | ✅ | `PatientMessage` threaded + `/api/v1/messages/` |
| PH.6.3#04 | ◐ | `PersonalRepresentative` captured/enforced but no render API |
| PH.6.3#07 | ✅ | Bidirectional messaging exchange implemented |
| PH.6.3#08 | ◐ | Access-scoped + audited; no confidentiality-level tagging |

### S / TI.4 / TI.5 — Interoperability & Terminology
| Criterion | V | Evidence |
|---|---|---|
| S.3.6#10 | ◐ | Provenance + audit; no cryptographic non-repudiation |
| TI.4.1#01 | ✅ | Codes resolved vs OMOP `Concept` (LOINC/SNOMED/RxNorm/ICD10CM/CVX) |
| TI.4.1#05 | ✅ | OMOP vocab tables + load commands + browse/search/graph endpoints |
| TI.4.1#10 | ✅ | OMOP↔FHIR system mapping on import & export |
| TI.4.2#01 | ◐ | `vocabulary_version` stored; single active version only |
| TI.4.2#02 | ✅ | `load_athena_vocabularies --replace` reloads a release |
| TI.4.2#05 | ✗ | No vocabulary-level deprecation status |
| TI.4.2#06 | ✅ | Per-code `invalid_reason`/`valid_end_date`; traversal honors it |
| TI.4.2#07 | ✗ | No template/formulary embedded-term substitution |
| TI.4.2#08 | ◐ | Terminology updatable/served; no content-authoring layer |
| TI.4.2#09 | ◐ | Per-code dates but no version change-history (`--replace` truncates) |
| TI.4.3#01 | ✅ | `ConceptRelationship 'Maps to'` used in prod; `SourceToConceptMap` |
| TI.5.1.1#01 | ✅ | FHIR R4 import/export + OAuth/SMART |
| TI.5.1.1#02 | ✅ | SMART discovery; client_credentials; Epic R4 |
| TI.5.1.1#03 | ◐ | Depends on full TI.4 — capped by TI.4.2 |
| TI.5.1.1#06 | ✅ | Coded terminologies both directions |
| TI.5.1.1#11 | ✅ | `AuditEvent` per exchange + `ProvenanceRecord` |
| TI.5.2#01 | ✗ | FHIR R4 only; no multi-version interchange |
| TI.5.2#02 | ◐ | App API versioned + deprecation headers; no config-driven standard adoption |
| TI.5.3#01 | ✅ | OAuth/OIDC+SMART, DRF REST, Module Federation remote, FHIR |
| TI.5.4#01 | ◐ | `OrgTrust`/OAuth scopes; no formal agreement artifact |
| TI.5.5#01 | ✅ | FHIR EHR/LIS/pharmacy ingest + HK-Labs sync; client_credentials |

### TI.1 — Security
| Criterion | V | Evidence |
|---|---|---|
| TI.1.1#01 | ✅ | JWT/OIDC, HMAC service token, OAuth2/SMART, local email+password, sessions |
| TI.1.1#02 | ✅ | Django PBKDF2 hashing; `set_unusable_password` for OIDC; `hmac.compare_digest` |
| TI.1.1#03 | ✗ | No failed-login lockout (rate-limit only; `login_view` unthrottled) |
| TI.1.1#04 | ✗ | No password-reuse timeframe / expiry |
| TI.1.1#05 | ✗ | No password history |
| TI.1.1#06 | ◐ | Validators configured but **bypassed on signup/invite** (len<8 only) |
| TI.1.1#07 | ✅ | `type="password"` fields; passwords never returned |
| TI.1.1#08 | ◐ | Inherited admin reset URL, UI link removed; no app-level reset workflow |
| TI.1.1#09 | ✗ | No force-change-at-next-logon |
| TI.1.1#10 | ✅ | Generic 'Invalid credentials'; dummy-hash timing equalization |
| TI.1.1#11 | ✅ | `email__iexact` case-insensitive usernames |
| TI.1.1#12 | ✅ | Django hashing preserves password case |
| TI.1.2#01 | ✅ | `GroupAccess` roles; SMART scopes; permission classes; `can_access_patient` |
| TI.1.2#02 | ✅ | Authz actions/denials audited via `AuditEvent` (status_code) |
| TI.1.2#03 | ✅ | Roles + contexts (self/representative/professional) via `get_actor_role` |
| TI.1.2#04 | ◐ | Access trail only; no field-level before/after revision history |
| TI.1.7#01 | ✅ | Routing endpoints authenticate the peer (IsAuthenticated/scoped/HMAC) |
| TI.1.7#02 | ◐ | OAuth client lifecycle audited; no trusted source/destination status registry |

### TI.2 — Audit
| Criterion | V | Evidence |
|---|---|---|
| TI.2#01 | ✅ | Access to audit records controlled (scoped viewset; admin read-only) |
| TI.2#02 | ✅ | Deletion limited to prune command / admin permission (caveat: generic delete perm) |
| TI.2.1#01 | ◐ | Every `/api/`+`/o/` request audited; admin & background events off the path |
| TI.2.1#02 | ✅ | Who/when/what/outcome captured (caveat: list reads store no specific record id) |
| TI.2.1#03 | ✅ | Dual-write stdout + `AuditEvent` (caveat: best-effort post-response) |
| TI.2.1#04 | ✅ | `timestamp = timezone.now` |
| TI.2.2#01 | ✗ | Proprietary format; not RFC 3881 / ATNA / FHIR `AuditEvent` |
| TI.2.2#04 | ✗ | Audit-log access not audited (`/audit-events` skipped; admin un-audited) |
| TI.2.2.1#01 | ◐ | App-layer immutable but rows deletable; no tamper-evidence/WORM |
| TI.2.3#01 | ✅ | Read-only paginated review at `/api/v1/audit-events/` (caveat: raw JSON) |
| TI.2.3#02 | ✅ | `after`/`before` timestamp-window filtering |
| TI.2.3#04 | ✗ | No break-glass / emergency-access authorization |

## Appendix B — Remediation issues (all ✅ merged, 2026-07-27)
| Issue | PR | Covers |
|---|---|---|
| #301 | #309 | Password validators bypassed on signup/invite (security) — TI.1.1#06 |
| #302 | #310 | TI.1.1 auth controls: lockout, reuse policy, force-change, admin reset |
| #303 | #311 | TI.2 standards-based audit format + audit-log-access auditing + admin/background triggers |
| #304 | #312 | TI.2 audit indelibility/tamper-evidence + break-glass review access |
| #305 | #313 | TI.4.2 terminology maintenance: version history, deprecation, embedded-term substitution |
| #306 | #314 | TI.5/S.3.6/PH.2.3 exchange integrity & non-repudiation, interchange agreements |
| #307 | #316 | PH.1/PH.2/TI.1.2 account-holder data: entered-in-error, rendering, AD status, revision history |
| #308 | #309 + #317 | PH.6.3 communications: proxy-authorization render API + message confidentiality |
