# PROMOP Security and SOC2 Remediation Plan

Date: 2026-08-26

Inputs:

- `~/Downloads/CancerBot-Security-Audit-EN.pdf`
- `~/Downloads/CancerBot-SOC2-Gap-EN.pdf`

Scope: PROMOP only. CancerBot and EXACT findings are referenced only where they affect
shared SOC2 evidence or federation boundaries.

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
