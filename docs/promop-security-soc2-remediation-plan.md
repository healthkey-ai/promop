# Security and SOC2 Remediation Plan

Date: 2026-08-26 (re-scoped 2026-08-26)

Inputs:

- `~/Downloads/CancerBot-Security-Audit-EN.pdf`
- `~/Downloads/CancerBot-SOC2-Gap-EN.pdf`

The audit is organised in three sections — **CB**, **EXACT**, **PROMOP** — and each
finding cites the repository it was found in. That split matters more than it first
appears, so read "Scope" before working from any finding number.

## Scope

**The certification target is CancerBot's `promop` branch** (`cancerbot-org/cancerbot`).
That is the deployment being taken to SOC2. This document covers it, and treats the
standalone PROMOP service as an annex.

The PROMOP F1-F23 findings cite `patient_portal/*` and
`omop_core/services/wearable_parsers.py` — i.e. the `healthkey-ai/promop` repo, the
standalone PROMOP service. **CancerBot does not run most of that code.** It embeds PROMOP
as a wheel pinned in its `requirements.txt`, and that wheel ships only `omop_core`,
`omop_oncology`, `omop_genomics`:

```toml
[tool.setuptools.packages.find]
include = ["omop_core*", "omop_oncology*", "omop_genomics*"]
```

`patient_portal` is not packaged and not in CancerBot's `INSTALLED_APPS`. The wheel's own
header states it also excludes "the patient_portal-coupled runtime utilities
(authorization.py, ...) — those stay PROMOP-service-only and must not be called from a
host lacking patient_portal."

Consequences, which govern how the finding lists below are read:

- 21 of the 23 PROMOP findings are in code CancerBot never loads. Fixing them hardens the
  standalone PROMOP service. It does not change CancerBot's SOC2 posture.
- Only PROMOP F4/F23 (XXE in the Apple Health parser) sit in `omop_core`, which CancerBot
  does load — and they are **unreachable there**: the only caller is
  `patient_portal/api/views.py:4136`, which is not installed; `omop_core` ships no
  `urls.py`; and CancerBot's urlconf mounts only admin/chats/trials. Close them for
  CancerBot with that trace as evidence, and re-open the question if CancerBot ever adds a
  wearable upload.
- CancerBot's own exposure is the audit's **CB** section, plus the findings recorded below
  that the audit did not cover because it reviewed each repository in isolation.

## CancerBot PROMOP Branch — Primary SOC2 Target

Issues filed in `cancerbot-org/cancerbot` on 2026-08-26. None of these appear in the
security audit: it reviewed application authorization, not the delivery pipeline, the
dependency seam, or write-path completeness.

### Delivery pipeline and configuration

| Issue | Severity | Finding |
|---|---|---|
| #4919 | HIGH | Deploys to prod/promop/staging are not gated on the test suite — `docker_build` has no `needs:`, so a red suite still ships |
| #4920 | MEDIUM | `CORS_ALLOW_ALL_ORIGINS = True` on a PHI API, with `authorization` and `x-imp-token` in the header allowlist |
| #4921 | MEDIUM | No transport security at all — no HSTS, secure cookies, SSL redirect, or `SECURE_PROXY_SSL_HEADER` |
| #4922 | MEDIUM | Celery/Redis TLS uses `ssl.CERT_NONE` — `rediss://` with no certificate validation |
| #4923 | MEDIUM | Production frontend builds with `pnpm install --no-frozen-lockfile` |
| #4924 | MEDIUM | No dependency-audit, SAST, or secret-scanning gate in CI |
| #4926 | MEDIUM | `StrictHostKeyChecking=no` on every deploy incl. production; `.dockerignore` omits `.git`/`.env` |

Known and already tracked: #2637 (DEBUG) covers half of audit finding CB F2. The committed
`django-insecure-` SECRET_KEY at `cancerbot/settings.py:71` is the other half and is not
separately tracked.

### CancerBot ↔ PROMOP seam

The seam's authorization held up under tracing — `?person_id=` is checked against the
caller's own profile, impersonation re-verifies per request, and no OMOP entry point takes
a patient identifier from client input. The problems are elsewhere:

| Issue | Severity | Finding |
|---|---|---|
| #4927 | HIGH | Chatbot clinical answers never reach OMOP; under `MATCHER_PATIENT_SOURCE=promop` the matcher reads a record missing them |
| #4925 | MEDIUM | `promop-omop` pinned to an unreviewed commit on an unmerged fork branch (CC8.1) |
| #4928 | MEDIUM | PROMOP's Django admin ships with the wheel, adding unscoped editable PHI to CancerBot's `/admin/` |
| #4929 | MEDIUM | The PROMOP import boundary is documented but unenforced; the one PHI-writing endpoint bypasses it |
| #4930 | MEDIUM | Patient-facing projection is a deny-list over a model defined upstream in a pinned wheel |
| #4931 | LOW | `PatientInfoSerializer.user` is writable; only a hand-written `pop` prevents cross-patient reassignment |

#4927 is a data-integrity and patient-safety bug rather than a security one, but it is the
most consequential item found: trial matching runs on a profile missing whatever the
patient told the chatbot, silently.

### Audit CB findings

All filed 2026-08-26, one issue per audit finding ID, so the audit maps onto tracked
issues without a lookup table. Where two or three findings shared a root cause and a fix,
they were filed together and every ID appears in the title.

**HIGH**

| Issue | Audit ID | Finding |
|---|---|---|
| #4932 | F1 | Stored XSS via `dangerouslySetInnerHTML` in trial tooltips |
| #4933 | F3 | Any authenticated user can modify or delete any clinical Trial |
| #4934 | F4 | DOM XSS in knowledge-graph tooltips via d3 `.html()` |
| #4935 | F5 | Self-asserted navigator reads any patient's record by inviting them |
| #4936 | F6 | Unauthenticated signup with arbitrary email hijacks Google sign-in |
| #4937 | F7, F10, F28 | Researcher PHI access authorized by unverified self-chosen email |
| #4938 | F8 | OAuth adopts a pre-existing local account matched only on email |
| #4939 | F18 | Invitation token is a 14-day never-invalidated takeover credential |
| #4940 | F29 | Researchers self-grant the flag that unmasks applicant identities |
| #2637 | F2 | DEBUG hard-coded True (pre-existing issue; audit detail added as a comment) |
| #4772 | F9 | Navigator gets a stranger's profile from POST my-patients (pre-existing) |

**MEDIUM**

| Issue | Audit ID | Finding |
|---|---|---|
| #4941 | F11 | Epic OAuth tokens logged in cleartext |
| #4942 | F12 | TrialLabeledValue write/delete via the `admin_view` branch |
| #4943 | F13 | Reset/invite URLs with takeover tokens logged and sent to Sentry |
| #4944 | F14 | `?view=admin_view` exposes internal LLM prompts to any user |
| #4945 | F15 | Trial authoring gated only by a client-controlled localStorage role |
| #4946 | F16 | No rate limiting or lockout on password authentication |
| #4947 | F17, F24 | Login timing side channel enables account enumeration |
| #4948 | F19 | CSV formula injection in the trials export |
| #4949 | F20, F21 | Prompt injection into the Standard-of-Care prompt |
| #4950 | F22 | `StudyInfoSerializer.owner` writable |
| #4951 | F23 | Type confusion on `supportive_therapies` persistently 500s search |
| #4952 | F26, F27 | One-time auth tokens shipped to Mixpanel via URL capture |
| #4953 | F30 | Prompt injection into the SoC comparison prompt |
| #4954 | F31 | Prompt injection into CRC question generation |

**LOW**

| Issue | Audit ID | Finding |
|---|---|---|
| #4955 | F25 | `javascript:` scheme injection in a trial-details anchor href |
| #4956 | F32 | Google sign-in ignores the ID token's `email_verified` claim |

Three cross-cutting themes are worth treating as single pieces of work rather than 32
independent fixes:

1. **Email is treated as an authenticated identity when nothing verifies it.** F6, F7, F8,
   F10, F28, F32 all reduce to this, and #4936/#4937/#4938 cannot be closed independently.
   A verified-email flow plus provider-`sub` matching plus a uniqueness constraint closes
   the set. This is the same defect class as PROMOP F1/F5/F8, already fixed in the PROMOP
   service — the reasoning there transfers.
2. **Self-asserted roles.** `role` at signup, `is_researcher`, and
   `has_researcher_contract` are all attacker-settable and all gate PHI. F5, F10, F29.
3. **Trial free text is attacker-writable and flows into both HTML and LLM prompts.** F3 is
   the write primitive; F1, F4, F20, F21, F30, F31 are its sinks. Fixing F3 reduces the
   severity of six other findings.

## PHR Coordination

PROMOP's audience and verified-email enforcement both need a change in
`healthkey-ai/phr` before they can be deployed. Filed and assigned:

- `healthkey-ai/phr#65` — phr emits no `aud` claim (`SIMPLE_JWT` has no `AUDIENCE`, and the
  introspection response omits it), so PROMOP will reject every phr token once its change
  deploys. Suggested shared value `promop-api`; PROMOP sets `PHR_AUDIENCE`, phr sets
  `JWT_AUDIENCE`. Note that a single global audience unblocks PROMOP without closing
  sibling-to-sibling replay — that residual risk should be a decision, not an accident.
- `healthkey-ai/phr#66` — phr emits no `email_verified` claim, so every phr user is treated
  as unverified: no link to an existing patient record on first login, and invited
  clinicians' org grants never transfer. phr's `identity_level` (`ial1` = "Email verified")
  may serve, but it is the default on every account and no confirm-email flow was found, so
  mapping it may not satisfy the finding.

Neither is on CancerBot's critical path.

## Current PROMOP Control Baseline

PROMOP is ahead of the global CancerBot SOC2 snapshot in a few areas:

- `DEBUG` defaults to false in `ctomop/settings.py`.
- Production CORS is restricted by `CORS_ALLOWED_ORIGINS`.
- Production secure-cookie, HSTS, content-sniffing, frame-deny, and proxy SSL settings exist.
- DRF throttling is configured for anonymous, user, sync, patient-sync, signup, and OMOP write buckets.
- Partner/service authentication rejects inactive identities.
- PROMOP has tamper-evident, hash-chained audit events for API/Admin/OAuth access.

These still need evidence or hardening:

- CI should run `manage.py check --deploy` under production-like settings.
- CI should add dependency audit, SAST, and secret scan gates.
- `AUDIT_HMAC_KEY` and `EXPORT_SIGNING_KEY` should be required independent managed keys in production, not fall back to `SECRET_KEY`.
- Branch protection, CODEOWNERS, access reviews, incident response, backup/restore, retention, sub-processor, encryption-at-rest, and recurring pentest evidence must be collected outside code.

## Prioritized Code Remediation

### P0: Broken Authorization and Identity Trust

1. Harden partner email trust and patient linking.
   - Issue: #746.
   - Findings: PROMOP F1, F5, F8.
   - Require verified email before email-based patient linking or placeholder org-grant migration.
   - Preserve `(issuer, sub)` as the primary login identity.
   - Prevent silent rebinding of an existing `PatientUser` from one identity to another.
   - Add regression tests for unverified-email tokens and verified-email tokens.
   - Deployment note: `email_verified` is now load-bearing for three behaviours —
     populating `Identity.email`, migrating placeholder `GroupAccess` invite
     grants onto a real login identity, and resolving an existing `Person`.
     Confirm the PHR and Firebase issuers actually emit the claim. If an issuer
     omits it, every login from it is treated as unverified and fails *silently*:
     `identity.email` is never set, an invited clinician's org grants never move
     off the placeholder identity so they sign in with no org access, and each
     login auto-provisions a duplicate `Person`. Only the address written to
     `Person.email` is now gated too, so the duplicates do not also poison the
     email lookup for the real owner.

2. Fix lab-result measurement and visit mutation authorization.
   - Issue: #747.
   - Findings: PROMOP F3, F6, F7, F9.
   - Keep read paths governed by read predicates.
   - Gate `PATCH`/`DELETE` measurement operations and visit deletion on write predicates.
   - Fail closed for user-less non-service OAuth2 callers.
   - Add tests for analyst read/no-write and unauthenticated principal fail-closed behavior.

3. Fix sync write authorization and actor spoofing.
   - Issue: #745.
   - Findings: PROMOP F12, F13, F14, F15.
   - Replace on-behalf-of write checks that use `can_access_patient` with `can_write_patient`.
   - Allow explicit body actor override only for trusted service-token calls.
   - For non-service callers, derive the write principal and provenance actor from the authenticated caller/token context.
   - Add tests proving analysts cannot write and body actor spoofing is rejected or ignored.

4. Fix `export-fhir` object authorization.
   - Issue: #744.
   - Finding: PROMOP F2.
   - Apply the same org/professional/patient enforcement used by patient-detail resolution before exporting a person by URL id.
   - Add cross-org and patient-self tests.

5. Fix CSV upload per-row tenancy checks.
   - Issue: #748.
   - Finding: PROMOP F11.
   - Apply per-person write authorization for every CSV row before creating/updating `Person` or clinical OMOP rows.
   - Reject org-scoped callers that name a patient in another organization.
   - Add mixed-row tests to ensure the entire upload fails without partial cross-tenant writes.

### P1: Token, Key, and Upload Hardening

6. Enforce PHR JWT audience validation.
   - Issue: #750.
   - Findings: PROMOP F10, F16, F17.
   - Configure an expected audience and require it for RS256 PHR token verification.
   - Add tests for wrong-audience rejection.
   - Deployment note: `PHR_AUDIENCE` must be set in the Render and GCP
     environments before this change reaches production. It has no
     production default — an unset value fails CLOSED and rejects every
     PHR token, so PHR federation logins break until the variable is set.
   - Blocked on `healthkey-ai/phr#65`: phr does not currently emit an `aud`
     claim at all, so setting `PHR_AUDIENCE` is necessary but not sufficient.
     Until phr ships its side, PHR authentication fails whatever the value.

7. Require dedicated production signing keys.
   - Issue: #749.
   - Findings: PROMOP F20 and SOC2 CC7.2 non-repudiation concern.
   - Keep development fallback behavior, but fail production configuration if `AUDIT_HMAC_KEY` or `EXPORT_SIGNING_KEY` is absent.
   - Document rotation procedure.

8. Tighten token cache posture.
   - Finding: PROMOP F21.
   - Keep the cache short, env-configurable, and covered by tests.
   - If provider revocation checks become available, bypass cache for revoked-token-sensitive providers or lower production TTL.

9. Harden Apple Health XML parsing.
   - Issue: #751.
   - Findings: PROMOP F4, F23.
   - Parse untrusted XML with a parser configuration that disables DTD/entity expansion rather than relying on header scanning.
   - Add tests with leading comments before a DTD.

10. Harden FHIR ingest type validation.
    - Finding: PROMOP F18.
    - Reject non-numeric `Observation.valueQuantity.value` with a 400 response instead of an unhandled exception.

11. Fix login response for non-portal accounts.
    - Issue: #755.
    - Finding: PROMOP F19.
    - Avoid confirming valid credentials for identities that cannot use the portal.
    - Return a generic authentication failure or an explicit non-sensitive enrollment-required response.

12. Constrain break-glass scope.
    - Issue: #755.
    - Finding: PROMOP F22.
    - Require an organization nexus or explicit emergency authorization policy before allowing break-glass for arbitrary `person_id`.
    - Ensure every grant is audited with reason and expiry.

## SOC2 Control Work

### Code-Backed Controls

1. CI security gates.
   - Issue: #754.
   - Add `manage.py check --deploy` with production-like env.
   - Add `pip-audit` or equivalent dependency audit.
   - Add Semgrep/Bandit SAST.
   - Add Gitleaks with a checked-in baseline for known test fixtures.

2. Change-management evidence.
   - Issue: #752.
   - Add `CODEOWNERS`.
   - Configure protected branch requirements in GitHub: PR review, green CI, no direct pushes.
   - Store branch-protection screenshots or exported settings in the SOC2 evidence repository.

3. Audit-key evidence.
   - Issue: #749.
   - Set and rotate `AUDIT_HMAC_KEY` separately from `SECRET_KEY`.
   - Set and rotate `EXPORT_SIGNING_KEY` separately from `SECRET_KEY`.
   - Run and retain output from `verify_audit_integrity`.

### Operator Evidence

All operator-evidence work is tracked in issue #753.

1. Access management.
   - Maintain onboarding/offboarding procedures.
   - Perform periodic production, database, cloud, and PHI access reviews.
   - Enforce MFA for staff, GitHub, cloud, database, and deploy accounts.

2. Incident response and recovery.
   - Maintain incident-response runbook and incident log.
   - Define RTO/RPO.
   - Run scheduled backup restore tests and retain evidence.

3. Data protection and vendor management.
   - Maintain sub-processor inventory and DPAs for analytics, Sentry, email, cloud, LLM providers, and hosting.
   - Document PHI retention/deletion policy.
   - Document encryption-at-rest attestation.
   - Inventory and control local production dumps.

4. Security testing.
   - Schedule recurring independent penetration testing.
   - Track remediation issues to closure.

## Implementation Branches

Main integration branch:

- `promop-security-soc2-remediation`

Initial parallel implementation slices:

- Lab-results mutation authorization.
- Partner email verification and PHR audience validation.
- FHIR/lab sync write authorization and actor provenance.
- CI/config controls and documentation.

The first implementation pass should land P0 authorization fixes before P1 hardening and SOC2 evidence-only work.
