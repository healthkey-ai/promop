# PROMOP Security and SOC2 Remediation Plan

Date: 2026-08-26

Inputs:

- `~/Downloads/CancerBot-Security-Audit-EN.pdf`
- `~/Downloads/CancerBot-SOC2-Gap-EN.pdf`

Scope: the PROMOP service (`healthkey-ai/promop`) only. CancerBot and EXACT findings are
referenced only where they affect shared SOC2 evidence or federation boundaries.

## Read this before working from a finding number

The SOC2 **certification target is CancerBot's `promop` branch**
(`cancerbot-org/cancerbot`), not this service. Its remediation plan lives in that
repository, at `docs/security-soc2-remediation-plan.md`.

The PROMOP F1-F23 findings cite `patient_portal/*` and
`omop_core/services/wearable_parsers.py` — this repository. **CancerBot does not run most
of that code.** It embeds PROMOP as a pinned wheel that ships only `omop_core`,
`omop_oncology`, `omop_genomics`:

```toml
[tool.setuptools.packages.find]
include = ["omop_core*", "omop_oncology*", "omop_genomics*"]
```

`patient_portal` is not packaged and not in CancerBot's `INSTALLED_APPS`. The wheel's own
header states it also excludes "the patient_portal-coupled runtime utilities
(authorization.py, ...) — those stay PROMOP-service-only and must not be called from a
host lacking patient_portal."

So:

- 21 of the 23 PROMOP findings are in code CancerBot never loads. Closing them hardens
  this service — worth doing on its own terms, and done in #756 — but it does not move
  CancerBot's SOC2 posture.
- Only PROMOP F4/F23 (XXE in the Apple Health parser) sit in `omop_core`, which CancerBot
  does load, and they are **unreachable there**: the only caller is
  `patient_portal/api/views.py:4136`, which is not installed; `omop_core` ships no
  `urls.py`; and CancerBot's urlconf mounts only admin/chats/trials. Close them for
  CancerBot with that trace as evidence, and revisit if CancerBot ever adds a wearable
  upload.
- CancerBot pins this repository at a commit that is not an ancestor of `dev`, so fixes
  landed here do not reach it until the pin moves. Tracked as
  `cancerbot-org/cancerbot#4925`.

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

## Status as of 2026-08-26

All five P0 items and four of the seven P1 items are merged in `6170982` (PR #756) and
their issues are closed. What remains is either blocked on a partner service or is
evidence work rather than code.

| Item | Issue | Status |
|---|---|---|
| P0 1 — partner email trust and patient linking | #746 | **Done** (PHR path gapped, see below) |
| P0 2 — lab measurement/visit mutation authz | #747 | **Done** |
| P0 3 — sync write authz and actor spoofing | #745 | **Done** |
| P0 4 — `export-fhir` object authorization | #744 | **Done** |
| P0 5 — CSV per-row tenancy | #748 | **Done** |
| P1 6 — PHR JWT audience validation | #750 | Code complete, **blocked on `healthkey-ai/phr#65`** |
| P1 7 — dedicated production signing keys | #749 | **Done** — keys set in hosted envs; rotation runbook at `docs/signing-key-rotation.md`; `start.sh` now runs `check --deploy` |
| P1 8 — token cache posture (F21) | #759 | **Done** — cache honours the token's `exp` and no entry outlives its token |
| P1 9 — Apple Health XML parsing | #751 | **Done** |
| P1 10 — FHIR ingest type validation | #751 | **Done** |
| P1 11 — login response for non-portal accounts | #755 | **Done** |
| P1 12 — break-glass scope | #755 | **Done** |
| SOC2 1 — CI security gates | #754 | **Done** — `check --deploy`, `pip-audit`, bandit (baselined), gitleaks |
| SOC2 2 — change-management evidence | #752 | **Partial** — `CODEOWNERS` added; branch protection requiring code-owner review is a repo setting, not code |
| SOC2 3 — audit-key evidence | #749 | **Done** in code and procedure; retaining `verify_audit_integrity` output is the recurring operator step |
| Operator evidence | #753 | **Not started** |

Three things the P0 work turned up that were not in this plan, all fixed in the same PR:

1. `upload_fhir` committed the writes it had just denied. The per-patient savepoint opened
   before the Person/Location/Death/Visit writes, the role gate ran after them, and the
   denial path exited with `continue` — which raises nothing, so the `finally` block handed
   `Atomic.__exit__` no exception and it committed. Regression test verified to fail on all
   four assertions with the fix reverted.
2. Three further write endpoints authorized on the read predicate: `bulk_delete`,
   `PersonViewSet.partial_update`, `EpisodeEventViewSet.perform_create`. `can_access_patient`
   grants the analyst role; `can_write_patient` does not.
3. `get_request_org` handed org-wide write trust to any org-linked application regardless of
   grant type. Now restricted to `client_credentials`, which fails closed.

Two carried gaps, deliberately not fixed and recorded on the closed issues:

- **#746 / PHR path.** PHR emits no `email_verified` claim, so every PHR user is treated as
  unverified: no link to an existing record on first login, and invited clinicians' org
  grants never transfer. Tracked as `healthkey-ai/phr#66`. The Firebase path is correct.
- **#748 / legacy rows.** A `PatientRecord` with `organization=None` can be claimed by
  whichever org's CSV upload touches that `person_id` first. Not a violation of the
  acceptance criterion — an unowned record is not "another organization" — and the obvious
  fix would break legitimate adoption of legacy data.

## Prioritized Code Remediation

### P0: Broken Authorization and Identity Trust

1. Harden partner email trust and patient linking. **[Done — #746]**
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

2. Fix lab-result measurement and visit mutation authorization. **[Done — #747]**
   - Issue: #747.
   - Findings: PROMOP F3, F6, F7, F9.
   - Keep read paths governed by read predicates.
   - Gate `PATCH`/`DELETE` measurement operations and visit deletion on write predicates.
   - Fail closed for user-less non-service OAuth2 callers.
   - Add tests for analyst read/no-write and unauthenticated principal fail-closed behavior.

3. Fix sync write authorization and actor spoofing. **[Done — #745]**
   - Issue: #745.
   - Findings: PROMOP F12, F13, F14, F15.
   - Replace on-behalf-of write checks that use `can_access_patient` with `can_write_patient`.
   - Allow explicit body actor override only for trusted service-token calls.
   - For non-service callers, derive the write principal and provenance actor from the authenticated caller/token context.
   - Add tests proving analysts cannot write and body actor spoofing is rejected or ignored.

4. Fix `export-fhir` object authorization. **[Done — #744]**
   - Issue: #744.
   - Finding: PROMOP F2.
   - Apply the same org/professional/patient enforcement used by patient-detail resolution before exporting a person by URL id.
   - Add cross-org and patient-self tests.

5. Fix CSV upload per-row tenancy checks. **[Done — #748]**
   - Issue: #748.
   - Finding: PROMOP F11.
   - Apply per-person write authorization for every CSV row before creating/updating `Person` or clinical OMOP rows.
   - Reject org-scoped callers that name a patient in another organization.
   - Add mixed-row tests to ensure the entire upload fails without partial cross-tenant writes.

### P1: Token, Key, and Upload Hardening

6. Enforce PHR JWT audience validation. **[Code complete — blocked on `healthkey-ai/phr#65`]**
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
   - `healthkey-ai/phr#66` is the matching gap for the verified-email work
     (P0 item 1): phr emits no `email_verified` claim, so every phr user is
     currently treated as unverified.

7. Require dedicated production signing keys. **[Done — #749]**
   - Issue: #749.
   - Findings: PROMOP F20 and SOC2 CC7.2 non-repudiation concern.
   - Keep development fallback behavior, but fail production configuration if `AUDIT_HMAC_KEY` or `EXPORT_SIGNING_KEY` is absent.
   - Document rotation procedure.

8. Tighten token cache posture. **[Done — #759]**
   - Finding: PROMOP F21.
   - Keep the cache short, env-configurable, and covered by tests.
   - If provider revocation checks become available, bypass cache for revoked-token-sensitive providers or lower production TTL.

9. Harden Apple Health XML parsing. **[Done — #751]**
   - Issue: #751.
   - Findings: PROMOP F4, F23.
   - Parse untrusted XML with a parser configuration that disables DTD/entity expansion rather than relying on header scanning.
   - Add tests with leading comments before a DTD.

10. Harden FHIR ingest type validation. **[Done — #751]**
    - Finding: PROMOP F18.
    - Reject non-numeric `Observation.valueQuantity.value` with a 400 response instead of an unhandled exception.

11. Fix login response for non-portal accounts. **[Done — #755]**
    - Issue: #755.
    - Finding: PROMOP F19.
    - Avoid confirming valid credentials for identities that cannot use the portal.
    - Return a generic authentication failure or an explicit non-sensitive enrollment-required response.

12. Constrain break-glass scope. **[Done — #755]**
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
