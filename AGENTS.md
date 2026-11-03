# Databases

The local `.env` holds two Render databases and deliberately **no `DATABASE_URL`**:

- `PROD_DATABASE_URL` is the **production** database (`promop`, `promop-worker`). Read-only: never write to production data; production is loaded from a staging dump, so fix and verify on staging.
- `STAGING_DATABASE_URL` is the **staging** database (`promop-staging`, `promop-staging-worker`). Use it for migrations, data fixes and investigation.

Django reads only `DATABASE_URL`, so every command sets it: `DATABASE_URL="${STAGING_DATABASE_URL:?}"` for staging, `DATABASE_URL="${PROD_DATABASE_URL:?}"` for production reads. **For local tests, always point `DATABASE_URL` at local PostgreSQL** (`postgresql://postgres@localhost:5433/promop_test`): pytest and `manage.py test` create their test database on whatever server it names, so never run them with it exported at staging or production.

# Environment conventions

- Staging always means Render: https://promop-staging.onrender.com.
- The staging web service is `promop-staging`; its worker is `promop-staging-worker`.
- Do not use the old Google Cloud / Cloud Run staging deployment to investigate or verify staging unless the user explicitly asks for it.
- Render production is `promop` with worker `promop-worker`. The unused legacy Render web service has been retired; do not include it in active readiness work.
- Cloud Run staging remains managed by `.github/workflows/deploy-staging.yml` and `Dockerfile.gcp`. Preserve its existing service, image, bucket and integration identifiers.
- `STAGING_DATABASE_URL` identifies Render staging; verify it matches the active Render web and worker configuration before database operations.
- See `docs/render-staging-celery.md` for Render staging configuration and verification.

# Pull request reviews

- There is no team review process. Do not request team reviewers (including `healthkey`) or configure required team-review rules.
- Request individual reviewers as directed by the user. Lars Burgess is `@larsburgess` on GitHub.
- Merging to `dev` does not require review approval. Individual review requests are optional; required CI checks must still pass.

# Pull request labels

- When an issue has a security-related label, apply the same label to its pull request.

## Documentation-only changes

- Documentation-only changes do not require application CI or local application
  test suites before push or merge. Review the content and check links and
  `git diff --check` instead.
- This is a standing user preference: do not run pytest, Django, frontend or
  async e2e suites for documentation-only changes, and do not ask the user to
  approve skipping them again.
- For an authorized documentation-only merge, run any lightweight docs-only
  status gates GitHub requires and complete the merge without asking for another
  confirmation. These status gates do not require application test results.
- Do not use `[skip ci]` for a protected-branch PR when it would leave required
  statuses missing and block the merge. Let the existing docs-only detector
  skip the application jobs and satisfy the required status gates instead.
- CI runs a lightweight file-change detector and skips backend, frontend,
  security, and async e2e jobs when every changed path is documentation. Keep
  the named suite jobs so GitHub records them as skipped. The required
  `Application CI` gate passes documentation-only PRs without requiring frontend
  or backend test results; code changes must pass the applicable application
  suites. `Security gates` remains a separate required check.
- Root Markdown/reStructuredText/AsciiDoc, documentation under `docs/`, and
  Markdown documentation under `.github/` qualify. Code, tests, configuration,
  dependency files, and runtime data do not qualify, even when changed alongside
  docs. The three generated crossmap Markdown artifacts under `docs/` are runtime
  data and continue to require CI.
- Unrecognized paths, an unavailable diff, or detector failure retain normal CI.
  GitHub-managed CodeQL scanning is separate from the application CI workflow.

## Backend CI scope

- Frontend-only changes (including recognized frontend assets/configuration and
  accompanying documentation) skip the backend suites. Backend, dependency,
  runtime-data, shared configuration, mixed, and unknown changes run them.
- Django and pytest run in isolated parallel jobs. The required `Backend tests`
  check succeeds only when both pass; detector failures retain full coverage.
- CI is the gate for the full suites. Before a push, run only the tests the
  change touches (plus `npm run lint` and `npm run build` for frontend work);
  do not run the full Django or pytest suite locally. Wait for the required
  checks before merging. See "Targeted Tests Before a Push" in `CLAUDE.md`.

## Migration conflicts

Two branches that each add an `omop_core` migration off the same base are each
green on their own, but once both merge, `dev` has two leaf migrations. Django
then refuses to migrate, the conflict check and the migration-graph tests fail,
and the staging deploy is skipped until someone repairs it. This has broken
`dev` repeatedly (e4ee0a1a, 97d3767a, 5dca221f, #1833, 82e12773, #1871). A PR's
CI result describes the base it ran against, not `dev` at the moment you merge,
so a stale green check does not catch it.

- **Rebase before creating a migration.** `git fetch origin dev && git rebase
  origin/dev`, then run `makemigrations`, so the migration gets the next number
  and depends on the current leaf.
- **Rebase again immediately before merging a PR that adds a migration.** If
  `omop_core/migrations/` on `dev` changed since the branch's last CI run,
  rebase, check the graph, push, and wait for CI to pass on the new head. Do
  not merge on the older green result.
- **Check the graph locally after every rebase.** With `DATABASE_URL` pointed at
  local PostgreSQL and `DEBUG=True`, run
  `.venv/bin/python manage.py makemigrations --check --dry-run`. It fails with
  "Conflicting migrations detected" when there are two leaves.
- **Regenerate an unmerged migration rather than merge around it.** If your
  migration is not on `dev` and has not been applied to staging, delete it,
  rebase, and run `makemigrations` again so it sits on the current leaf.
  Hand-edit its `dependencies` only if it contains data migration code that
  `makemigrations` cannot regenerate.
- **Never renumber or rename a migration that is on `dev` or applied to
  staging.** Django keys on the full name, so a renamed migration looks like it
  never ran and would be applied a second time (see e4ee0a1a). Rejoin the leaves
  with a merge migration instead.
- **When `dev` already has two leaves**, fix it at once on a branch: add one
  empty merge migration whose `dependencies` list both leaves, with a docstring
  naming the PRs that forked the graph (as in
  `0295_merge_source_code_upload_and_facility_review`). Check that nobody has
  already opened the same fix, because two identical merge migrations create a
  new pair of leaves (#1833).
- **Merge migration PRs one at a time.** When several open PRs add
  migrations, merge one, then rebase the next onto the new `dev`, check it, and
  let CI pass before merging it.

## Documentation naming and ownership

- Use `_architecture.md` for implemented architecture and `_plan.md` for future
  work, delivery status, dependencies and acceptance gates.
- Keep field/value inventories in generated reference artifacts. Architecture
  documents explain behavior; plans link to inventory evidence instead of
  duplicating per-field tables or deployment receipts.
