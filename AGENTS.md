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

## Documentation naming and ownership

- Use `_architecture.md` for implemented architecture and `_plan.md` for future
  work, delivery status, dependencies and acceptance gates.
- Keep field/value inventories in generated reference artifacts. Architecture
  documents explain behavior; plans link to inventory evidence instead of
  duplicating per-field tables or deployment receipts.
