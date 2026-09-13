# Change management evidence — CC8.1

This procedure covers `healthkey-ai/promop` changes to `dev` and `main`, including
application code, migrations, CI, deployment configuration, and these controls. The
evidence is configuration observed at a stated time, not proof that the controls
operated throughout a SOC 2 observation period.

## Required controls

Both branches must require a pull request and these CI checks:

- `Backend tests`
- `Frontend lint & build`
- `Security gates`

Independent review is required only for security-related changes: PRs changing
security-sensitive files, or PRs with a security label or linked security-labelled
issue. Copy an issue's security-related label to its PR. Ordinary PRs may merge without
an approving review once the other required checks pass.

One approving review of the current head from **any other developer with repository
write access** satisfies the security review requirement. Maintain and admin access also
qualify. The author, bots, read-only collaborators, stale approvals and dismissed
approvals do not qualify. An outstanding request for changes from another writer blocks
approval. Membership in a particular GitHub team is not required.

The `Security review` status implements this conditional requirement in [the policy
script](../../.github/scripts/security-review.cjs). It checks PR labels, closing issues
and issue references in the PR title/body, plus security-sensitive files (including
renamed-away paths). `security`, case-insensitive `security:` and `security-` labels
count. Test-only paths do not trigger file-based review; a security label still requires
approval. The script lists the exact file patterns.

The proposed ruleset has **zero** global approvals, global last-push approval
**disabled**, code-owner approval **disabled**, and **no team-specific reviewers**. It
requires `Security review` from the GitHub Actions app, alongside existing CI. Dismiss
stale approvals when the reviewed diff changes and resolve review conversations. Retain
signed commits, linear history, force-push restrictions, and no ruleset bypass actors.

`CODEOWNERS` routes review requests using the target branch's copy. A review request
alone is not a mandatory approval; the catch-all owner does not impose review on
ordinary PRs or limit eligible security reviewers.

## Rollout and current enforcement

The [2026-09-13 capture](evidence/2026-09-13-change-management-security-review.json)
shows the existing partial rollout: global approvals are already zero, but native
security file rules still require the `healthkey` team and no `Security review` status
is required. Issue labels alone currently have no automatic review gate. This does
**not** satisfy the any-other-developer policy yet. The older proposal
[#1219](https://github.com/healthkey-ai/promop/pull/1219) was closed without merging;
this PR includes the replacement workflows and tests.

1. Obtain independent approval for this security-related PR and merge the policy
   workflows/script into the trusted default branch. The existing native rule
   remains in effect during bootstrap.
2. Dispatch `security-review.yml` and retain successful workflow execution plus
   statuses for open PRs. Confirm an ordinary PR passes without review and a
   security PR needs current approval from another writer.
3. Re-read the live ruleset and apply
   [the proposed payload](evidence/change-management-ruleset.json), preserving any
   unrelated changes since capture. In the same update, remove team-specific
   `required_reviewers` and require `Security review` with GitHub Actions as its
   expected source (integration ID `15368`). Do not require an unavailable status.
4. Re-capture with `--require-enforced` and retain the new dated evidence. Verify
   both `dev` and `main`, including approval by a writer outside `healthkey`.

The status writer executes only trusted default-branch code with metadata read and
status-write permissions. It never checks out PR code. A separate unprivileged
review-event workflow signals trusted reevaluation. PR/issue events and a five-minute
scheduled reconciliation refresh results; scheduled runs can be delayed. API errors fail
closed. PRs sharing a head are evaluated together so an ordinary PR cannot overwrite a
security PR's failed status. Label/review changes can take until the next successful run
to update a previously published status.

The ruleset change must preserve unrelated rules. Capture the complete existing ruleset
before editing, review the proposed JSON in the PR, apply it through the GitHub API or
Settings, and capture the effective settings again. An administrator can still edit
repository rules; record those security-related changes in a separate reviewed PR and
retain the corresponding organization audit-log entries where available.

## Deployment authorization

Staging means Render `promop-staging` and `promop-staging-worker`, tracking `dev`.
Production is the Render production web/worker pair declared in `render.yaml`, tracking
`main`. The Blueprint declares automatic deployments. The protected merge, with
independent approval when the change is security-related, authorizes deployment to the
corresponding environment; there is no claim of a separate Render approval prompt.

For a normal staging change, retain the linked issue, PR, commit/diff, CI results, and
merge SHA. For security-related changes, also retain the approving identity and
timestamp. Record the Render web and worker deployment IDs and deployed SHAs, then
record the relevant smoke-test result. A GitHub check passing does not prove that either
Render process deployed successfully.

For production, use a promotion PR to `main`. Record authorization covering the
production release: scope, tested staging SHA, migrations and prerequisites, release
window when needed, rollback plan, and a named release owner. The release owner records
authorization in the PR before merging. Independent PR review is required when the
promotion contains security-related changes. Follow the additional release-specific
gates in [the release runbook](../release-1.2-dev-to-main-runbook.md). A production
merge may immediately start a Render deployment.

For a manual deploy, redeploy, rollback, or dashboard/Blueprint configuration change,
record the target service, exact commit or configuration diff, reason, validation and
rollback plan, and named approval in the linked issue or release PR **before** executing
it. Record the operator, timestamp, Render deployment ID, and outcome afterwards. A
deploy hook or API credential is an execution credential, not approval evidence.
Restrict those credentials and Render access to authorized operators.

The captured GitHub environments currently have no required deployment reviewers. Render
integration-created GitHub environment records do not establish an approval gate in
Render. The repository also contains a legacy Cloud Run staging workflow; its existence
or success is not evidence for Render staging. This procedure does not operate or verify
that legacy deployment.

## Evidence collection and retention

From a checkout with `gh` authenticated for read access to repository settings:

```bash
python3 scripts/capture_change_management_evidence.py \
  --repo healthkey-ai/promop \
  --output docs/soc2/evidence/YYYY-MM-DD-change-management.json \
  --require-enforced
```

The collector reads rulesets (including inherited sources), each branch's effective
rules and head SHA, the selected CODEOWNERS path/blob and validation, GitHub environment
protection rules, and workflow inventory. It never reads secrets or Render credentials.
It exits nonzero if the required security-review status/CI or direct-push protection
cannot be demonstrated, if blanket approval gates apply to ordinary PRs, if
team-specific reviewers remain, or if CODEOWNERS validation reports errors. API errors
fail the capture; missing access must not be described as a passing control.

Keep the dated baseline and subsequent captures; do not replace an earlier snapshot with
new settings. Commit each new capture through a PR, with independent review for these
security-related control changes, and retain the PR, CI and deployment records with the
SOC 2 evidence for the audit period. Re-capture after changes to rules, owners, required
check names, or deploy permissions, and during the recurring access/control review.
Retain external Render configuration and deploy records in the organization's evidence
store; they are outside this collector's scope.

The baseline for [issue #752](https://github.com/healthkey-ai/promop/issues/752) is [the
2026-09-12 snapshot](evidence/2026-09-12-change-management-before.json). It records zero
required approvals and an administrator `always` bypass. The [later 2026-09-12
capture](evidence/2026-09-12-change-management-enforced.json) records the former blanket
one-approval/last-push policy. Both snapshots are historical; neither describes the
current security-only review policy.

The [proposed ruleset payload](evidence/change-management-ruleset.json) describes the
target configuration; it is not yet applied. The 2026-09-13 capture records the
remaining status-gate and team-restriction gaps described above. The collector checks
ruleset configuration, not the correctness of workflow code or approvals on individual
PRs. Retain workflow, test, status and review records as well.

This control-change PR is security-related (issue #752 is labelled `security` and the PR
changes CODEOWNERS), so it still needs independent approval. Retain its review and merge
records with these snapshots; author self-review does not satisfy that requirement.

## References

- [GitHub ruleset controls and bypass permissions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [GitHub CODEOWNERS behavior](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
- [Render deployment triggers](https://render.com/docs/deploys)
- [Render staging operation and verification](../render-staging-celery.md)
