# 1.3.0 release candidate preparation

**Temporary handoff document — delete after the final 1.3.0 release ships.**
Do not delete when an RC is tagged. Move any unfinished work back to its GitHub
issue and retain release evidence in the release notes or release PR before
removing this document and any links to it.

Created: 2026-09-14. Status: proposed priorities; implementation and release
verification remain outstanding unless explicitly recorded below.

## Purpose and scope

Prepare `1.3.0-rc1` around clinical correctness and release readiness. This plan
comes from reviewing 172 open issues, recent merges, issue comments, and focused
code checks. It is a triage snapshot, not evidence that the candidate is ready.
Recheck issue and PR status when resuming; several open issues already have
substantial fixes merged.

Staging always means Render: <https://promop-staging.onrender.com>. The web
service is `promop-staging`; the worker is `promop-staging-worker`. Follow
[the Render staging guide](render-staging-celery.md). Do not use the old Google
Cloud / Cloud Run staging deployment for this work.

## Recommended implementation order

- [ ] **1. Correct breast cancer mappings — [#1227](https://github.com/healthkey-ai/promop/issues/1227), under [#21](https://github.com/healthkey-ai/promop/issues/21).**
  Fix the wrong-code paths through which HER2 can populate Ki-67, ER can populate
  Oncotype/methodology, and perineural invasion can populate nodal status. These
  paths were still visible in the inspected code. Ship focused corrections and
  regression tests first; #1227 explicitly permits clear-cut wrong-code fixes
  before the common mapping runtime dependency. Include an impact report for
  existing data. Keep the parent issues open until their full acceptance criteria
  are met.
- [ ] **2. Normalize ANC and platelet units — [#640](https://github.com/healthkey-ai/promop/issues/640).**
  Cover import, derivation, fallback paths, and legacy aliases. Values supplied in
  cells/µL currently reach columns labeled thousands/µL without conversion,
  creating a potential 1,000× error in trial eligibility inputs. Verify equivalent
  source units yield the same canonical values and handle missing/unsupported
  units explicitly.
- [ ] **3. Finish browser authentication hardening — [#141](https://github.com/healthkey-ai/promop/issues/141).**
  Integrate [PR #1216](https://github.com/healthkey-ai/promop/pull/1216), which
  replaces browser tokens with server sessions and rejects retired browser-client
  credentials. Reported tests pass on that PR; revalidate after integration,
  including login/logout and federation. Follow its deployment instructions for
  any custom retired OAuth client IDs.
- [ ] **4. Finish disease-specific assessments and coverage — [#1260](https://github.com/healthkey-ai/promop/issues/1260).**
  Complete [PR #1267](https://github.com/healthkey-ai/promop/pull/1267) and Render
  staging verification. Include FLIPI/GELF assessment semantics, unknown versus
  assessed zero/negative, FL grades 3A/3B, and disease-specific controls. Complete
  sample coverage and record before/after evidence for meaningful acceptance
  testing.
- [ ] **5. Resolve remaining genomics evidence semantics — [#1240](https://github.com/healthkey-ai/promop/issues/1240).**
  Core dialog/state work has merged through #1255/#1249. The remaining release
  concern is the legacy TP53/del(17p) aggregate returning false without qualifying
  evidence. Resolve unknown-versus-negative behavior with downstream consumers
  and the clinical/source decisions in
  [#1246](https://github.com/healthkey-ai/promop/issues/1246). Do not invent clinical
  thresholds or treat a gene-only negative as structural absence. Verify absent,
  indeterminate, no-evidence, and contradictory-source cases.

## Additional release gates

- [ ] **PHR authentication — [#750](https://github.com/healthkey-ai/promop/issues/750).**
  PROMOP audience validation is implemented and fails closed. Partner
  [phr#65](https://github.com/healthkey-ai/phr/issues/65) was still open at triage.
  If PHR federation is in the candidate's supported scope, ship the partner
  capability, confirm matching audience configuration, and prove authentication
  works end to end. Do not weaken validation to make login work.
- [ ] **Genomics schema compatibility — [#1237](https://github.com/healthkey-ai/promop/issues/1237).**
  Render staging passed the width audit with zero oversized values; see the
  [2026-09-14 evidence](https://github.com/healthkey-ai/promop/issues/1237#issuecomment-5658503062).
  Finish inventorying intended upgrade environments and checking NOTE integrity.
  Implement a lossless forward repair only where evidence requires one. The
  staging result does not establish another deployment's state or NOTE integrity.
- [ ] **Patient editing acceptance — [#1066](https://github.com/healthkey-ai/promop/issues/1066).**
  The reported inherited Org Admin permission defect was fixed through #1251 and
  PR #1252. Verify authorized mapped and unmapped saves persist, mapped edits
  project correctly, and interactive saves do not invoke reverse derivation.
  Retain the [PatientRecord-first architecture](patient-record-first-writes.md);
  use this as an acceptance check rather than reopening the architecture.

## Conditional scope: coded-value interoperability

Decide whether coded-value interoperability is a headline promise of 1.3.0.
If it is, include this dependency chain:

1. [#1223 — Field/value inventory](https://github.com/healthkey-ai/promop/issues/1223).
   At handoff, [PR #1271](https://github.com/healthkey-ai/promop/pull/1271) is open;
   review its actual coverage before considering this prerequisite complete.
2. [#1224 — Stable scoped choices and reviewed answer mappings](https://github.com/healthkey-ai/promop/issues/1224).
3. [#1226 — Coded-answer projection and readback](https://github.com/healthkey-ai/promop/issues/1226).
4. Scoped reconciliation and verification from
   [#1231](https://github.com/healthkey-ai/promop/issues/1231), coordinated with its
   listed dependencies. A scoped release task does not close the entire issue.

Otherwise, defer the full [#26 mapping programme](https://github.com/healthkey-ai/promop/issues/26)
while fixing the confirmed wrong-code defects above. Do not claim full coded-value
interoperability in the release notes without the corresponding acceptance evidence.

## Work to defer

New webhooks (#42 / PR #1220), Phase 2 genomics imports and shared test/specimen
work (#1243–#1245), broader MCL parity, and cosmetic work should not expand the RC
critical path. Keep their existing issues open.

## Resume on another machine

1. Fetch the repository and open this document from `docs/release-1.3-rc-plan.md`.
   Until merged, it is on branch `docs/release-1.3-rc-plan`, targeting `dev`.
2. Recheck linked issue comments, PR status, and current `dev`. The checkboxes here
   represent remaining work at handoff, not a live mirror of GitHub.
3. Confirm the supported release scope, especially PHR federation and coded-value
   interoperability. Record decisions and evidence below as work proceeds.
4. Work through the priorities in order, preserving unrelated local changes in
   separate worktrees where needed. Update this document in commits so the next
   machine can resume from the same state.
5. Establish a clean candidate from the intended `dev` commit and record its exact
   SHA. Require CI and migration, login, patient-edit, import, and worker smoke
   checks on that exact candidate on Render. Repeat affected checks if it changes.
   Verify both fresh installation and supported upgrade paths. Record deployment
   IDs, migration state, vocabulary prerequisites, and recovery instructions.
6. Follow repository review rules: no required team review and no team reviewer
   requests. Merging to `dev` does not require review approval; required CI must
   still pass. Respect applicable checks and protection on the release target.

## Progress and release evidence

| Item | Decision / evidence |
| --- | --- |
| Supported release scope | Pending |
| Candidate branch and SHA | Pending |
| Priority fixes and PRs | Pending |
| Remaining known limitations | Pending |
| CI and migration evidence | Pending |
| Render deployment and smoke evidence | Pending |
| Final 1.3.0 release URL | Pending |

- [ ] After the **final 1.3.0 release** ships, preserve useful evidence in the
  release notes/PR, transfer unfinished tasks to their issues, and **delete this
  temporary document and any links to it**.
