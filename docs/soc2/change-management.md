# Change management evidence — CC8.1

This procedure covers `healthkey-ai/promop` changes to `dev` and `main`, including
application code, migrations, CI, deployment configuration, and these controls. The
evidence is configuration observed at a stated time, not proof that the controls
operated throughout a SOC 2 observation period.

## Required controls

Both branches must require a pull request and these CI checks:

- `Application CI` — a gate job that conditionally enforces backend and frontend
  suites based on the change scope. Documentation-only PRs pass without running
  application suites. Frontend-only PRs skip backend tests. Code changes that
  touch backend files require both suites to pass.
- `Security gates`

Independent review is required only for security-related changes: PRs changing
security-sensitive files, or PRs with a security label or linked security-labelled
issue. Copy an issue's security-related label to its PR. Ordinary PRs may merge without
an approving review once the other required checks pass.

One approving review of the current head from **@larsburgess** (GitHub user ID
`23724`), with repository write, maintain or admin access, satisfies the security
review requirement. The author, other developers, bots, read-only collaborators,
stale approvals and dismissed approvals do not qualify. An outstanding request for
changes from another reviewer blocks approval.

The `Security review` status implements this conditional requirement in [the policy
script](../../.github/scripts/security-review.cjs). It checks PR labels, closing issues
and issue references in the PR title/body, plus security-sensitive files (including
renamed-away paths). `security`, case-insensitive `security:` and `security-` labels
count. Test-only paths do not trigger file-based review, except where they fall inside
one of the exceptions enumerated below; a security label still requires approval. The
script lists the exact file patterns.

The proposed ruleset has **zero** global approvals, global last-push approval
**disabled**, and code-owner approval **disabled**. It requires `Security review` from
the GitHub Actions app, alongside `Application CI` and `Security gates`. Dismiss stale
approvals when the reviewed diff changes and resolve review conversations. Retain signed
commits, linear history, force-push restrictions, and no ruleset bypass actors.

`CODEOWNERS` routes review requests using the target branch's copy. A review request
alone is not a mandatory approval; the catch-all owner does not impose review on
ordinary PRs. Security paths route to @larsburgess; the status independently verifies his identity and current-head approval.

## Rollout and current enforcement

The [2026-09-13 capture](evidence/2026-09-13-change-management-security-review.json)
shows the existing partial rollout: global approvals are already zero, but no
`Security review` status is required yet. Issue labels alone currently have no
automatic review gate. The older proposal
[#1219](https://github.com/healthkey-ai/promop/pull/1219) was closed without merging;
this PR includes the replacement workflows and tests. The repository owner selected
@larsburgess as the required individual reviewer. The trusted named-reviewer status
enforces this after bootstrap.

1. Obtain @larsburgess approval for this security-related PR and merge the policy
   workflows/script into the trusted default branch. The existing native rule
   remains in effect during bootstrap.
2. Dispatch `security-review.yml` and retain successful workflow execution plus
   statuses for open PRs. Confirm an ordinary PR passes without review and a
   security PR needs current approval from @larsburgess. An approval from any other writer must fail.
3. Re-read the live ruleset and apply
   [the proposed payload](evidence/change-management-ruleset.json), preserving any
   unrelated changes since capture. Require `Security review` with GitHub Actions
   as its expected source (integration ID `15368`). Do not require an unavailable
   status.
4. Re-capture with `--require-enforced` and retain the new dated evidence. Verify
   both `dev` and `main`, including rejection of another writer’s approval and
   acceptance of Lars’s current approval.

The status writer executes only trusted default-branch code with metadata read and
status-write permissions. It never checks out PR code, and it checks out only the policy
module itself: the policy's tests run in a separate job that holds no write permissions,
so no test file executes in the job that publishes the verdict. Five groups are
classified as security-sensitive even when named like tests: files under `.github/`,
CODEOWNERS, `docs/soc2/` and the scanner configurations, because a test file in the
control plane still runs with the control plane's privileges; the change-management
evidence script and its package; the two Django project packages `promop/` and
`ctomop/` **whole**, because a `test_settings.py` there is still a settings module and
the packages also hold the `DJANGO_SETTINGS_MODULE` selector that decides which
settings production loads; and the deployment tests named under "Scope changes to the
required review" below, which are what makes leaving the operational files ungated
defensible. A separate unprivileged
review-event workflow signals trusted reevaluation. PR/issue events and a five-minute
scheduled reconciliation refresh results; scheduled runs can be delayed. API errors fail
closed: a commit that cannot be evaluated is marked `failure`, and if even that
status cannot be written the workflow run itself fails so the stale verdict is
visible. PRs sharing a head are evaluated together so an ordinary PR cannot overwrite a
security PR's failed status. A commit's status is written only when the verdict changes,
which keeps the five-minute reconciliation from exhausting GitHub's per-commit status
limit. Label/review changes can take until the next successful run
to update a previously published status.

The ruleset change must preserve unrelated rules. Capture the complete existing ruleset
before editing, review the proposed JSON in the PR, apply it through the GitHub API or
Settings, and capture the effective settings again. An administrator can still edit
repository rules; record those security-related changes in a separate reviewed PR and
retain the corresponding organization audit-log entries where available.

## Scope changes to the required review

Narrowing the set of paths that require @larsburgess approval is itself a change
to this control, so each narrowing is recorded here with its rationale and the
risks accepted. The authoritative list stays in
[the policy script](../../.github/scripts/security-review.cjs); this section says
why it holds the shape it does.

### 2026-09-24 — narrowed to auth/identity, settings and credential handling (#1496)

The gate had grown to fire on most PRs, which makes a required review less
meaningful rather than more: a reviewer who is asked to approve every data
endpoint stops reading any of them closely. These path groups were removed:

| Removed | Reason |
|---|---|
| `**/*urls.py` | Every new data endpoint tripped the gate. The two project URLconfs (`promop/urls.py`, `ctomop/urls.py`) stay gated, as part of their packages. The seven app URLconfs carry no permission decisions today, and are reached only through the gated project URLconf, under a DRF `DEFAULT_PERMISSION_CLASSES` of `['IsAuthenticated']` — a convention rather than a guarantee, since a plain Django view added to one would have no permission default either. |
| `.env*`, `**/.env*` | Secrets are not committed — `.gitignore` excludes `.env` and `.env.*` with an explicit exception for `.env.example`, whose values are documented placeholders. |
| `render.yaml`, `start*.sh`, `ops/artemis/Dockerfile` | Operational, and pinned by the tests named below. |
| `**/Dockerfile*` (root and `.gcp`), `docker-compose*.yml`, `Procfile`, `nixpacks.toml` | Operational, and **not** pinned. See accepted risks. |

Dependency manifests (`requirements*.txt`, `frontend/package*.json`) were
considered for removal and deliberately **kept**: `pip-audit` and Dependabot fire
on a known advisory against a dependency already present, and say nothing about
one being *added*. A new direct dependency, or a name one character from a real
one, is the reviewable event and no scanner raises it.

**Compensating control.** The ungated operational files are left out on the
grounds that their security-relevant values are pinned by tests, and the pinning
tests are themselves inside `CONTROL_PATHS` — so a file and the assertion
constraining it cannot be relaxed in the same ungated change. The pins are:

- `tests/test_web_startup.py` — the exact command sequence in `start.sh`,
  including `manage.py check --deploy --fail-level ERROR`, and that `start.sh`
  leaves `DJANGO_SETTINGS_MODULE` unset so the gated selectors in
  `promop/wsgi.py` and `manage.py` decide which settings production loads.
- `tests/test_render_blueprint_invariants.py` — stated over the whole blueprint
  rather than per named service, so an appended service does not arrive
  unpinned. It holds for every service, including ones that do not exist yet:
  `DEBUG` must be `False` (a value-less entry fails, so it cannot be moved to
  the dashboard), every broker must declare an empty `ipAllowList`, no database
  may declare a non-empty one, no `SERVICE_AUTH_SCOPES` may grant a
  resource-wide write scope, and every web service must start through
  `start.sh`. The `DEBUG` rule reaches only a service that declares a `DEBUG`
  entry; a new service omitting the key entirely is not checked.

  Its host and origin rule is narrower, and the difference matters: the wildcard
  check skips entries with no `value`, because a `sync: false` entry is supplied
  in the Render dashboard and the blueprint cannot see it. Production declares
  both `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` that way, so **their production
  values are not reviewed by anything in this repository** — the test rejects
  only a wildcard written into the blueprint, which is what staging does. What
  does hold for production is weaker: `tests/test_render_staging_blueprint.py`
  asserts the keys are declared at all, and `tests/test_render_production_settings.py`
  asserts the application refuses to boot when either is missing (for
  `ALLOWED_HOSTS` under both a gunicorn and a `check --deploy` argv; for
  `CORS_ALLOWED_ORIGINS` the test covers `check --deploy` only, though the guard
  in `promop/settings.py` fires for any http-serving argv).

- `tests/test_deployment_startup_contract.py` — the ordering inside `start.sh`,
  the bans on `seed_omop_concepts` and `load_athena_vocabularies` appearing in
  it, and `ATHENA_VOCABULARY_GDRIVE_URL` on the web service.
- `tests/test_render_staging_blueprint.py` — additionally executes
  `start-worker.sh`, pinning its required-env failures and concurrency defaults.
  `start-worker.sh` also left the gate via `start*.sh` and is the staging
  worker's `startCommand`.
- `tests/test_artemis_runtime.py` — `ops/artemis/Dockerfile`: `USER artemis`,
  no `EXPOSE`, pinned base image and `ARTEMIS_REF`.

**Accepted risks.** The compensating control reaches `render.yaml`, `start.sh`,
`start-worker.sh` and `ops/artemis/Dockerfile`. It does not reach the following,
which are therefore both ungated and unpinned. Stated so this list is not read
as covering more than it does:

- **`nixpacks.toml` — the sharpest of these.** It declares its own start
  command, `python manage.py migrate && python manage.py seed_omop_concepts &&
  python manage.py setup_admin && gunicorn promop.wsgi:application …`. That
  command runs **no `check --deploy`** at all, and it runs `seed_omop_concepts`,
  which `tests/test_deployment_startup_contract.py` explicitly bans from
  `start.sh`. Nothing in the repository asserts anything about this file, so a
  deployment target using it bypasses the deploy-check control entirely.
- **`Procfile`** — zero assertions anywhere in the repository.
- **`docker-compose.yml`, `docker-compose.dev.yml`** — zero assertions.
  `docker-compose.bridge.yml` has one, in `tests/test_project_package_compatibility.py`,
  which is itself ungated.
- **The root `Dockerfile`** — zero assertions of any kind.
- **`Dockerfile.gcp`** — two assertions exist (`promop.wsgi:application`,
  `npm run build:remote`), but they live in `tests/test_project_package_compatibility.py`,
  which is **not** in `CONTROL_PATHS`. The premise above — that a file and the
  assertion constraining it cannot be relaxed in one ungated change — does not
  hold here: both can move together, unreviewed.
- `.dockerignore` has no pin, so a change to what enters the build context is
  unreviewed.
- No test constrains a service's `buildCommand`.
- Production `ALLOWED_HOSTS` and `CORS_ALLOWED_ORIGINS` are dashboard-supplied,
  so their values change outside this repository and outside this control
  entirely. Only their absence is caught, at boot.
- Of the two scanners, only gitleaks reaches these files: it runs
  `detect --source . --no-git` over the whole tree, so a credential committed in
  a Dockerfile, `.dockerignore` or `render.yaml` is caught. Bandit does not —
  CI runs it as `bandit -r omop_core patient_portal promop ctomop omop_oncology
  omop_genomics`, which is Python packages only. Neither tool says anything
  about these files beyond the committed-secret case.

**Approval.** #1496 changes `.github/**` and `CODEOWNERS`, so it required
@larsburgess approval under the control it modifies. Record the approving review
and the merge commit alongside the evidence capture for the period.

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
cannot be demonstrated, if blanket approval gates apply to ordinary PRs, or if
CODEOWNERS validation reports errors. API errors fail the capture; missing access must
not be described as a passing control.

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
remaining status-gate gaps described above. The collector checks
ruleset configuration, not the correctness of workflow code or approvals on individual
PRs. Retain workflow, test, status and review records as well.

This control-change PR is security-related (issue #752 is labelled `security` and the PR
changes CODEOWNERS), so it needs approval from @larsburgess. Retain its review and merge
records with these snapshots; author self-review does not satisfy that requirement.

## References

- [GitHub ruleset controls and bypass permissions](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets)
- [GitHub CODEOWNERS behavior](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners)
- [Render deployment triggers](https://render.com/docs/deploys)
- [Render staging operation and verification](../render-staging-celery.md)
