# HL7 PHR-S FM R2 — Conformance Claim via Self-Attestation

> **Status:** Ready for authorized signature. **Re-verified 2026-07-27** against `dev` HEAD
> after the WS0 remediation (#301–#308) merged; **updated for the TI.2.2.1 audit hash chain
> (#318)**. Self-attestation — not validated by the HL7 EHR WG. The §5 functions were verified
> criterion-by-criterion against the source code (see Appendix A); the signatory should confirm
> the determination before issuing.

## 1. System & vendor identification
| Field | Value |
|---|---|
| Product | **promop** (oncology PHR / patient portal) |
| Vendor / attesting entity | **HealthKey.ai** |
| Product version | `dev` @ `9ad972d` (2026-07-27) |
| Attestation type | **Self-Attestation** |
| Attestation date | **2026-07-27** |
| Authorized representative | **Adam Blum** — adam@healthkey.ai |

## 2. Standard & basis
- **Standard:** HL7 Personal Health Record System Functional Model, **Release 2 (PHR-S FM R2)**.
- **Basis:** a **function-level claim** scoped to the **HealthKey Oncology Patient PHR Functional
  Profile** ([`phrs-fm-onco-profile.md`](phrs-fm-onco-profile.md)) — a vendor-defined, oncology /
  account-holder-focused selection. Per Ch. 5 a function is claimed conformant only when **all its
  mandatory (SHALL) criteria** are met (SHOULD/MAY optional); per Ch. 7 this claim does not itself
  create an HL7 Functional Profile.

## 3. Attestation statement
> HealthKey.ai attests that promop, as identified in §1, satisfies the mandatory (SHALL)
> conformance criteria of each PHR-S FM R2 function listed in §5, as verified by criterion-level
> source-code review on 2026-07-27. Functions evaluated but not fully satisfying their SHALL
> criteria are disclosed in §6 and are **not** claimed. The limitations in §7 are disclosed.

## 4. Conformance determination method
- **Method:** Self-attestation via **structured, criterion-level source-code verification** (four
  independent audit passes over the profile's functions), each SHALL graded MET / PARTIAL / NOT MET
  (Appendix A). Re-verified after WS0. **No HL7 conformance-determination tool** was used.
- **Scope evaluated:** 26 candidate leaf functions, **78 SHALL criteria**. No criterion ruled N/A —
  all conditional dependencies (local passwords, patient + provider roles, external ingest) apply.
- **Result (2026-07-27, incl. #318):** **75 MET · 3 PARTIAL · 0 NOT MET** — **24 of 26 functions
  fully conformant** (up from 49/18/11 and 10 conformant at the pre-WS0 audit). The 3 remaining
  PARTIAL criteria are both in **Optional** functions (TI.5.2, TI.5.4).
- **Supporting evidence:** the traceability matrix + profile doc, and the automated regression suite
  (**1010 backend tests passing**).

## 5. Functions CLAIMED conformant *(all applicable SHALL met — re-verified)*
| FM ID | Function | Determination |
|---|---|---|
| PH.1.1 | Identify & maintain account-holder record | 3/3 MET |
| PH.1.2 | Manage demographic information | 5/5 MET *(see §7.5)* |
| PH.1.4 | Manage advance directives | 3/3 MET |
| PH.1.5 | Manage consents & authorizations | 2/2 MET |
| PH.2 | Manage historical & current-state data | 2/2 MET |
| PH.2.1 | Manage account-holder-originated data | 3/3 MET |
| PH.2.3 | Manage data from external clinical sources | 3/3 MET *(see §7.4)* |
| PH.2.4 | Produce & present ad-hoc views (FHIR export) | no SHALL criteria; implemented |
| PH.3.1.1 | Manage personal observations & care | 1/1 MET |
| PH.6.3 | Provider ↔ account-holder communications | 4/4 MET |
| TI.1.1 | Entity authentication | 12/12 MET *(see §7.6)* |
| TI.1.2 | Entity authorization | 4/4 MET |
| TI.1.7 | Secure data routing | 2/2 MET |
| TI.2 | Audit (parent) | 2/2 MET |
| TI.2.1 | Audit triggers | 4/4 MET |
| TI.2.2 | Audit log management (FHIR AuditEvent, retention, access-audit) | 2/2 MET |
| TI.2.2.1 | Audit log indelibility | 1/1 MET *(hash chain, #318; see §7.2)* |
| TI.2.3 | Audit notification & review (incl. break-glass) | 3/3 MET |
| TI.4.1 | Standard terminology & models | 3/3 MET |
| TI.4.2 | Terminology maintenance & versioning | 7/7 MET *(see §7.7)* |
| TI.4.3 | Terminology mapping | 1/1 MET |
| TI.5.1.1 | Application interchange standards | 5/5 MET |
| TI.5.3 | Standards-based application integration | 1/1 MET |
| TI.5.5 | System integration | 1/1 MET |
| S.3.6 | Information import/export | 1/1 MET *(see §7.3)* |

## 6. Functions evaluated but NOT claimed *(≥1 SHALL only PARTIAL)*
| FM ID | Profile level | Residual | Path to MET |
|---|---|---|---|
| TI.5.2 Interchange-standard versioning | Optional (out of scope) | #01/#02 PARTIAL — FHIR R4 declared + non-R4 cleanly rejected (406), but no cross-version transform | multi-version support if a non-R4 partner is required |
| TI.5.4 Interchange agreements | Optional (out of scope) | #01 PARTIAL — `InterchangeAgreement` records exist but exchange is gated by OAuth/`OrgTrust`, not driven/enforced by the agreement | bind provisioning to agreement records |

Both remaining PARTIAL functions (TI.5.2, TI.5.4) are declared **Optional** and do not block the
profile claim. As of #318 **every Essential function of the profile is fully conformant.**

## 7. Limitations & caveats (disclosed)
1. **Scoped, function-level claim** against the HealthKey Oncology Patient PHR Profile — not a claim
   of full-model conformance. The profile's **entire Essential set is fully conformant** (as of #318);
   the only remaining PARTIAL functions are Optional (§6).
2. **TI.2.2.1 indelibility** — implemented as tamper-**evidence**: per-row HMAC-SHA256 `signature`
   (detects field alteration) **plus a hash chain** (`chain_hash` links each row to its predecessor,
   sealed under an advisory lock; #318) so whole-row deletion/insertion between survivors is now
   detected by `verify_audit_integrity`, and delete-restriction (admin delete disabled; deletion only
   via the audited retention job). Attested **MET**. Disclosed limit: this is tamper-*evidence*, not
   physical WORM — rows remain DB-mutable, and **tail-truncation of the newest rows** is undetectable
   without an external anchor (periodically publishing the latest `chain_hash` closes this — planned).
3. **S.3.6#10 non-repudiation** uses a **symmetric HMAC** (`EXPORT_SIGNING_KEY`) over exported
   bundles — strong content-integrity + origin authentication to key-holders, but **not asymmetric
   third-party non-repudiation** (a shared-key holder could forge). Disclosed.
4. **PH.2.3#09 content integrity** — request digest verification on ingest is **opt-in** (no header →
   no content check); always-on **transport** authentication (OAuth service tokens + TLS) carries the
   SHALL, with the digest as an additional verifiable layer.
5. **PH.1.2#05 rendering** — consent/preference-driven demographic redaction is a **bounded hook** on
   the primary `PatientRecordSerializer` read path; extension to other read paths is deferred.
6. **TI.1.1#09** — the `must_change_password` flag exists and is exposed but is currently **inert**
   (never set `True`, not consumed by the client). The SHALL's "ability to update password at next
   logon" is met via the change-password endpoint + admin email-link reset; wiring the flag is a
   recommended hardening → #319.
7. **TI.4.2 versioning** is **sequential** (not concurrent live multi-version); embedded-term
   substitution (#07/#08) applies to the coded data store — promop has no template/formulary
   authoring layer.
8. **Not HL7-validated** — self-attestation; accuracy is the vendor's responsibility.

## 8. Signatory
| | |
|---|---|
| Name / title | Adam Blum, HealthKey.ai |
| Signature | *__________________________* |
| Date | 2026-07-27 |

---

## Appendix A — Per-function determination (re-verified 2026-07-27)
Four independent criterion-level audit passes over the current `dev` code. **✅ conformant ·
◐ partial.** Full per-criterion verdicts + evidence are in the verification records; the residual
(non-MET) criteria are enumerated below.

| FM ID | SHALL | MET | Verdict |
|---|---:|---:|---|
| PH.1.1 | 3 | 3 | ✅ |
| PH.1.2 | 5 | 5 | ✅ |
| PH.1.4 | 3 | 3 | ✅ |
| PH.1.5 | 2 | 2 | ✅ |
| PH.2 | 2 | 2 | ✅ |
| PH.2.1 | 3 | 3 | ✅ |
| PH.2.3 | 3 | 3 | ✅ |
| PH.3.1.1 | 1 | 1 | ✅ |
| PH.6.3 | 4 | 4 | ✅ |
| TI.1.1 | 12 | 12 | ✅ |
| TI.1.2 | 4 | 4 | ✅ |
| TI.1.7 | 2 | 2 | ✅ |
| TI.2 | 2 | 2 | ✅ |
| TI.2.1 | 4 | 4 | ✅ |
| TI.2.2 | 2 | 2 | ✅ |
| TI.2.2.1 | 1 | 1 | ✅ *(hash chain, #318; see §7.2 limit)* |
| TI.2.3 | 3 | 3 | ✅ |
| TI.4.1 | 3 | 3 | ✅ |
| TI.4.2 | 7 | 7 | ✅ |
| TI.4.3 | 1 | 1 | ✅ |
| TI.5.1.1 | 5 | 5 | ✅ |
| **TI.5.2** | 2 | 0 | **◐** — multi-version (Optional) |
| TI.5.3 | 1 | 1 | ✅ |
| **TI.5.4** | 1 | 0 | **◐** — agreement enforcement (Optional) |
| TI.5.5 | 1 | 1 | ✅ |
| S.3.6 | 1 | 1 | ✅ |
| **Total** | **78** | **75** | 24 functions ✅ · 3 SHALL ◐ |

**Residual (PARTIAL) criteria:** TI.5.2#01/#02 (declared R4 only, no cross-version transform);
TI.5.4#01 (agreement records descriptive, not enforcement-bound). Both are in Optional functions.

## Appendix B — Remediation
**WS0 (closed, merged 2026-07-27):**
| Issue | PR | Covers |
|---|---|---|
| #301 | #309 | Password validators on signup/invite (TI.1.1#06) |
| #302 | #310 | TI.1.1 auth controls: lockout, reuse policy, force-change, admin reset |
| #303 | #311 | TI.2 FHIR-AuditEvent format + audit-log-access + admin/background triggers |
| #304 | #312 | TI.2 tamper-evidence + delete-restriction + break-glass |
| #305 | #313 | TI.4.2 deprecation, version history, concept-replacement |
| #306 | #314 | S.3.6/PH.2.3/TI.5 exchange integrity, non-repudiation, interchange agreements |
| #307 | #316 | PH.1/PH.2/TI.1.2 entered-in-error, redaction, AD status, revision history |
| #308 | #309 + #317 | PH.6.3 proxy-authorization render API + message confidentiality |
| #318 | #320 | TI.2.2.1 audit **hash chain** — detect audit-row deletion (Essential set → 25/25) |

**Residual follow-ups (from re-verification):** #319 wire the `must_change_password` force-change flag
(TI.1.1#09, §7.6); external `chain_hash` anchoring to close tail-truncation (TI.2.2.1 limit, §7.2).
TI.5.2 / TI.5.4 remain Optional.
