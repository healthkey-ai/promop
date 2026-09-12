# Change management evidence — CC8.1

This procedure covers `healthkey-ai/promop` changes to `dev` and `main`, including
application code, migrations, CI, deployment configuration, and these controls.
The evidence is configuration observed at a stated time, not proof that the
controls operated throughout a SOC 2 observation period.

## Required controls

Both branches must require a pull request, at least one approving reviewer with
write access other than the author, and these CI checks:

- `Backend tests`
- `Frontend lint & build`
- `Security gates`

Dismiss approvals when the reviewed diff changes, require approval of the latest
reviewable push, and resolve review conversations. Retain the existing signed
commit, linear history, and force-push restrictions. Do not grant ruleset bypass
exemptions: an administrator's `always` exemption permits direct pushes and makes
the displayed review and CI requirements optional for that administrator.

`CODEOWNERS` routes review requests. GitHub uses the target branch's copy, not the
PR's proposed copy. An author cannot approve their own PR. For the ownership
bootstrap, require one independent write-access reviewer; enable the additional
code-owner approval restriction only once the target branch includes an alternate
owner. In particular, adding an alternate on `dev` does not update `main`.
Do not bypass an approval requirement to land the ownership bootstrap.

The ruleset change must preserve unrelated rules. Capture the complete existing
ruleset before editing, review the proposed JSON in the PR, apply it through the
GitHub API or Settings, and capture the effective settings again. An administrator
can still edit repository rules; record those changes in a separate reviewed PR
and retain the corresponding organization audit-log entries where available.

## Deployment authorization

Staging means Render `promop-staging` and `promop-staging-worker`, tracking `dev`.
Production is the Render production web/worker pair declared in `render.yaml`,
tracking `main`. The Blueprint declares automatic deployments. The independent
PR approval and protected merge authorize deployment to the corresponding
environment; there is no claim of a separate Render approval prompt.

For a normal staging change, retain the linked issue, reviewed PR, approved
commit/diff, approving identity and timestamp, CI results, and merge SHA. Record
the Render web and worker deployment IDs and deployed SHAs, then record the
relevant smoke-test result. A GitHub check passing does not prove that either
Render process deployed successfully.

For production, use a promotion PR to `main`. Its approval must explicitly cover
the production release: scope, tested staging SHA, migrations and prerequisites,
release window when needed, rollback plan, and a named release owner. The release
owner records authorization in the PR before merging. Follow the additional
release-specific gates in [the release runbook](../release-1.2-dev-to-main-runbook.md).
A production merge may immediately start a Render deployment.

For a manual deploy, redeploy, rollback, or dashboard/Blueprint configuration
change, record the target service, exact commit or configuration diff, reason,
validation and rollback plan, and named approval in the linked issue or release
PR **before** executing it. Record the operator, timestamp, Render deployment ID,
and outcome afterwards. A deploy hook or API credential is an execution
credential, not approval evidence. Restrict those credentials and Render access
to authorized operators.

The captured GitHub environments currently have no required deployment reviewers.
Render integration-created GitHub environment records do not establish an
approval gate in Render. The repository also contains a legacy Cloud Run staging
workflow; its existence or success is not evidence for Render staging. This
procedure does not operate or verify that legacy deployment.

## Evidence collection and retention

From a checkout with `gh` authenticated for read access to repository settings:

```bash
python3 scripts/capture_change_management_evidence.py \
  --repo healthkey-ai/promop \
  --output docs/soc2/evidence/YYYY-MM-DD-change-management.json \
  --require-enforced
```

The collector reads rulesets (including inherited sources), each branch's
effective rules and head SHA, the selected CODEOWNERS path/blob and validation, GitHub environment
protection rules, and workflow inventory. It never reads secrets or Render
credentials. It exits nonzero if required review/CI or direct-push protection
cannot be demonstrated, or if CODEOWNERS validation reports errors. API errors
fail the capture; missing access must not be described as a passing control.

Keep the dated baseline and subsequent captures; do not replace an earlier
snapshot with new settings. Commit each new capture through a reviewed PR and
retain the PR, CI and deployment records with the SOC 2 evidence for the audit
period. Re-capture after changes to rules, owners, required check names, or deploy
permissions, and during the recurring access/control review. Retain external
Render configuration and deploy records in the organization's evidence store;
they are outside this collector's scope.

The baseline for [issue #752](https://github.com/healthkey-ai/promop/issues/752)
is [the 2026-09-12 snapshot](evidence/2026-09-12-change-management-before.json).
It records zero required approvals, code-owner review disabled, and an
administrator `always` bypass. It must not be presented as compliant enforcement.
The [applied ruleset payload](evidence/change-management-ruleset.json) records the
reviewable configuration change. The [enforced-state API capture](evidence/2026-09-12-change-management-enforced.json)
records the resulting settings: one independent approval, required CI, no bypass
actors, stale-approval dismissal and approval of the last reviewable push. Both
branches are included explicitly, in addition to the default-branch selector.
Code-owner-specific approval remains disabled for the ownership bootstrap
explained above; independent write-access review is enforced now.

Completion still requires an independently approved PR. A self-review comment
does not satisfy the approval control. Retain its review and merge records with
these snapshots; applying the rule is not evidence that a particular PR was
independently reviewed.

## References

- [GitHub ruleset controls and bypass permissions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [GitHub CODEOWNERS behavior](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
- [Render deployment triggers](https://render.com/docs/deploys)
- [Render staging operation and verification](../render-staging-celery.md)
