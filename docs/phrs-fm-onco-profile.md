# HealthKey Oncology Patient PHR — PHR-S FM R2 Functional Profile (DRAFT)

A **scoped Functional Profile** of the HL7 Personal Health Record System Functional
Model R2 (PHR-S FM R2) for **promop**, HealthKey.ai's oncology patient PHR. It selects
the account-holder-facing functions plus the core Trust-Infrastructure (security, audit,
terminology, interoperability) functions relevant to an oncology PHR, and declares the
conformance target for each.

> **Why a profile:** PHR-S FM conformance is inherently *profile-based* — a system
> "conforms to one or more Functional Profiles," never to the whole 748-SHALL model
> (Ch. 5). This profile lets HealthKey.ai make a **meaningful, defensible conformance
> claim now** — against a coherent, oncology-relevant subset — rather than waiting for
> full-model coverage. Documenting the profile (its function/criteria selection) is a
> recognized vendor pattern; the accompanying **self-attestation**
> ([`phrs-fm-conformance-claim.md`](phrs-fm-conformance-claim.md)) then attests against it.

**Profile:** HealthKey Oncology Patient PHR · **v0.1 (draft)** · basis: PHR-S FM R2 ·
prepared 2026-07-26.

**Status legend:** ✅ conformant (all in-scope SHALL met) · ◐ partial (gap → issue) ·
⏳ in progress · ○ not started · **E** Essential (required for the profile) ·
**O** Optional (declared out-of-scope for this profile).

---

## In-scope functions & conformance target

### Personal Health — account holder
| FM | Function | Level | Status | Gap → |
|---|---|---|---|---|
| PH.1.1 | Identify & maintain account-holder record | E | ◐ | entered-in-error (#307) |
| PH.1.2 | Manage demographics | E | ◐ | consent-driven rendering (#307) |
| PH.1.4 | Manage advance directives | E | ◐ | "in-effect" status (#307) |
| PH.1.5 | Manage consents & authorizations | E | ✅ | — |
| PH.2 | Manage historical & current-state data | E | ✅ | — |
| PH.2.1 | Account-holder-originated data | E | ✅ | — |
| PH.2.3 | Data from external clinical sources | E | ◐ | content-integrity on ingest (#306) |
| PH.2.4 | Produce & present ad-hoc views (FHIR export) | E | ✅ | — |
| PH.3.1.1 | Manage personal observations & care | E | ✅ | — |
| PH.6.3 | Provider ↔ account-holder communications | E | ◐ | confidentiality tagging (#308) |

### Trust Infrastructure — security
| FM | Function | Level | Status | Gap → |
|---|---|---|---|---|
| TI.1.1 | Entity authentication | E | ✅ | (lockout/reuse/force-change/reset — #301,#302) |
| TI.1.2 | Entity authorization | E | ◐ | field-level revision history (#307) |
| TI.1.7 | Secure data routing | E | ◐ | source/destination status audit (#306) |

### Trust Infrastructure — audit
| FM | Function | Level | Status | Gap → |
|---|---|---|---|---|
| TI.2 | Audit (parent) | E | ✅ | — |
| TI.2.1 | Audit triggers | E | ✅ | (admin+background — #303; not every command wired) |
| TI.2.2 | Audit log management (format, retention, access-audit) | E | ✅ | (#298,#303) |
| TI.2.2.1 | Audit log indelibility | E | ✅ | tamper-evidence + delete-restriction (#304); not physical WORM |
| TI.2.3 | Audit notification & review (incl. break-glass) | E | ✅ | (#304) |

### Trust Infrastructure — terminology & interoperability
| FM | Function | Level | Status | Gap → |
|---|---|---|---|---|
| TI.4.1 | Standard terminology & models | E | ✅ | — |
| TI.4.2 | Terminology maintenance & versioning | E | ⏳ | version history / deprecation (#305, in progress) |
| TI.4.3 | Terminology mapping | E | ✅ | — |
| TI.5.1.1 | Application interchange standards | E | ◐ | gated by TI.4.2 (#305) |
| TI.5.3 | Standards-based application integration | E | ✅ | — |
| TI.5.5 | System integration | E | ✅ | — |
| S.3.6 | Information import/export | E | ◐ | non-repudiation signing (#306) |
| TI.5.2 | Interchange-standard versioning | **O** | ○ | single FHIR R4 by design — out of scope |
| TI.5.4 | Interchange agreements | **O** | ○ | org-trust/OAuth scoping suffices — out of scope |

---

## Explicitly out of scope (with rationale)
Excluded from this profile — not required for an oncology *patient* PHR, and where the
bulk of the model's cost lives:
- **RI.1.1 record-lifecycle events** (52 functions / 278 SHALL) and the **granular
  TI.2.1.2/2.1.3 per-trigger audit functions** (~230 SHALL) — the 68% mega-cluster; the
  profile relies on the generic all-request audit trail (TI.2.1) instead of per-lifecycle-
  event evidence management.
- **S.1 provider info, S.2 financial, S.4 registries/research** (S.4.1 trial-matching is a
  roadmap candidate but not required here).
- **PH.4 health education, PH.5 decision support, PH.6.1–6.8** (beyond messaging).
- **TI.3 registry, TI.6 business rules, TI.7 workflow, TI.8 backup, TI.9 ops, TI.10
  clinical models** — platform/infra services.

---

## Current conformance snapshot (2026-07-26)
Of the **~26 Essential functions**:
- **✅ Conformant now: 15** — PH.1.5, PH.2, PH.2.1, PH.2.4, PH.3.1.1, TI.1.1, TI.2, TI.2.1,
  TI.2.2, TI.2.2.1, TI.2.3, TI.4.1, TI.4.3, TI.5.3, TI.5.5.
- **⏳ Landing:** TI.4.2 (#305, in progress).
- **◐ Remaining gaps: 10** — all mapped to open WS0 issues **#305, #306, #307, #308**.

Optional functions (TI.5.2, TI.5.4) are declared out-of-scope, so they do not block the
claim.

## Path to full profile conformance
Completing the remaining **WS0** issues closes every Essential gap in this profile:
| Issue | Closes (profile functions) |
|---|---|
| #305 | TI.4.2 (→ unblocks TI.5.1.1) |
| #306 | PH.2.3, TI.1.7, S.3.6 (integrity / non-repudiation) |
| #307 | PH.1.1, PH.1.2, PH.1.4, TI.1.2 (account-holder data) |
| #308 (remainder) | PH.6.3 (message confidentiality tagging) |

**Estimated effort to a fully-conformant Oncology PHR Profile: ~4–6 more full-time days**
(the rest of WS0) — versus ~4–6 months for full-model conformance. In other words,
**finishing WS0 ≈ completing this profile.**

## Caveats
- **Self-attestation** — not validated by the HL7 EHR WG; accuracy is the vendor's.
- **Vendor-defined profile** — documented here as a distinct artifact; the conformance
  *claim* does not itself create an HL7 Functional Profile (Ch. 7).
- TI.2.2.1 is tamper-**evidence** + delete-restriction, not physical WORM immutability.
- "Essential Now" priority has not been reconciled against the normative Function List;
  the Essential/Optional split above is HealthKey's profiling decision for oncology PHR use.
