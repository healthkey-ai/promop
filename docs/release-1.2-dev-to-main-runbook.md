# Release 1.2: dev-to-main promotion runbook

**Status:** in progress — resume at **Phase 0: establish and inspect the
refreshed candidate**.  Phase 2 local PostgreSQL migration validation is the
next substantive release gate.  Do not promote, tag, or deploy until every required gate below
has passed.

This is the release runbook for promoting the current `dev` integration branch
to `main`, then creating the annotated tag `v1.2.0` (release name: **Release
1.2**).  It is deliberately checkpointed so it can be resumed after an
interruption without relying on terminal history.

## Release baseline and scope

Record these before starting a phase.  If any remote ref has moved, stop and
rebase the release candidate from the new `origin/dev`, then repeat the gates
affected by the rebase.

| Item | Value at runbook creation |
| --- | --- |
| Candidate branch | `release-candidate/1.2-dev-to-main` |
| Candidate HEAD | `b0784b3` — deploy gate reapplied to refreshed `dev` |
| Candidate parent / current dev | `c8c20c0` (PR #979 merged) |
| Current main | `ccf3aff` |
| Promotion size | 496 commits ahead of main |
| Migration endpoint | `omop_core.0201_seed_hklabs_sccm` |
| Existing release tag convention | annotated-looking version tags: `v1.0.0`, `v1.1.0` |

The promotion contains schema and data migrations, code-mapping workflow and
artifacts, async derivation support, authentication/access work, deployment
configuration, and a large frontend change.  Treat it as a production release,
not a routine fast-forward.

### Important migration invariant

`0201_seed_hklabs_sccm` creates approved HK-Labs-to-LOINC mappings.  Its target
LOINC concepts must already exist.  For production-like validation and the
production deployment, the required order is:

1. migrate `omop_core` through `0200`;
2. load a full, in-scope Athena vocabulary bundle (including LOINC); and
3. apply the remaining migrations, including `0201`.

Never use `seed_omop_concepts` as a substitute for the full Athena bundle in a
deployed or release-validation database.

### Working-tree safety checkpoint

The former uncommitted mapping/vocabulary work was reviewed and merged to
`dev` in PR #977, with CI-isolation and LFS-checkout corrections merged in PR
#979. This fresh candidate includes both. The original dirty worktree is being
preserved separately; do not use it to prepare or tag the release.

Before merging, verify `git status --short` is clean in this release worktree.
Do not accidentally include any unrelated work in the release commit or tag.

## Phase 0 — establish a clean, current candidate

- [ ] Preserve or commit the working-tree changes according to the decision
  above; `git status --short` is clean in the release worktree.
- [ ] Fetch refs and record exact SHAs:

  ```bash
  git fetch origin --prune
  git rev-parse origin/main origin/dev HEAD
  git log --oneline --left-right origin/main...HEAD
  ```

- [ ] Confirm the candidate is based exactly on the intended `origin/dev`.
  If `origin/dev` advanced, merge or rebase it into the candidate, resolve
  conflicts with owners, and restart Phase 1.
- [ ] Inspect the release diff and the deployment-sensitive changes:

  ```bash
  git diff --stat origin/main...HEAD
  git diff origin/main...HEAD -- start.sh render.yaml .github/workflows \
    docker-compose.yml requirements.txt omop_core/migrations
  git diff --check origin/main...HEAD
  ```

- [ ] Confirm branch protection, required checks, and who is authorized to
  merge `main` and create the production tag.

**Resume evidence:** paste the three SHAs and the clean-status output into the
release PR description or release notes.

## Phase 1 — review the high-risk release boundary

- [ ] Review all migration files introduced since `main`, paying particular
  attention to the historical merged migration heads (`0154`, `0177`, `0179`,
  `0185`, `0200`) and data migrations (`0156`–`0201`). Confirm one graph leaf:

  ```bash
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py showmigrations omop_core --plan
  ```

- [ ] Inspect every `RunPython` migration for idempotence, reversibility (or a
  documented irreversible reason), runtime, locking, and its dependency on
  vocabulary data. In particular, verify `0201` cannot create null-target or
  duplicate mappings when LOINC is present.
- [ ] Review `start.sh` and the candidate's deploy gate against the migration
  invariant above. Confirm production has `ATHENA_VOCABULARY_GDRIVE_URL` (or
  the supported equivalent) pointing to the complete approved Athena export.
- [ ] Review configuration/dependency changes and frontend API contracts.
- [ ] Obtain review/sign-off for migration safety and deployment configuration.

**Stop condition:** any unresolved migration graph, data-loss risk, missing
Athena bundle, or incompatible production configuration blocks the release.

## Phase 2 — local PostgreSQL migration validation (current checkpoint)

Use an isolated local PostgreSQL database, never the configured remote
database. Substitute a fresh database name for `<release_db>`; do not point
these commands at a shared development, staging, or production database.

- [ ] Ensure PostgreSQL is running and the `postgres` role can create/use the
  temporary database. Create the database using the team's normal local setup.
- [ ] Enable the extension used by the schema if it is not already enabled:

  ```bash
  psql -U postgres -d <release_db> -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
  ```

- [ ] Prove migrations are generated and graph-consistent before executing:

  ```bash
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py makemigrations --check --dry-run
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py showmigrations omop_core --plan
  ```

- [ ] Apply through `0200` and record the elapsed time and resulting migration
  list:

  ```bash
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py migrate omop_core 0200 --noinput
  ```

- [ ] Load the full approved Athena bundle, including LOINC, using the same
  route intended for production (`--gdrive`, `--archive`, or `--path`). Record
  the bundle/version and loader result:

  ```bash
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py load_athena_vocabularies --path <athena_dir>
  ```

- [ ] Verify LOINC exists before the seed migration, then apply all remaining
  migrations:

  ```bash
  psql -U postgres -d <release_db> -c \
    "SELECT count(*) AS loinc_concepts FROM concept WHERE vocabulary_id = 'LOINC';"
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py migrate --noinput
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_db>" \
    .venv/bin/python manage.py showmigrations omop_core --plan
  ```

- [ ] Validate the seeded mapping rows: targets are non-null, target concepts
  exist and are LOINC, and no duplicate source keys exist. Adapt the source
  filter only if the migration's exact provenance value differs:

  ```sql
  SELECT count(*) AS null_targets
  FROM omop_core_sourcecodeconceptmapping
  WHERE target_concept_id IS NULL;

  SELECT count(*) AS missing_or_non_loinc_targets
  FROM omop_core_sourcecodeconceptmapping m
  LEFT JOIN concept c ON c.concept_id = m.target_concept_id
  WHERE m.target_concept_id IS NOT NULL
    AND (c.concept_id IS NULL OR c.vocabulary_id <> 'LOINC');
  ```

- [ ] Repeat `migrate --noinput`; it must be a no-op. Run `manage.py check
  --deploy --fail-level ERROR` with non-production placeholder secrets and
  hosts. Preserve the command output, elapsed time, Athena release identifier,
  row counts, and any warnings with the release evidence.

**Pass condition:** all migrations end at `0201`, the second migration run is a
no-op, LOINC was present before `0201`, checks pass, and the mapping validation
has no unexpected null, missing, non-LOINC, or duplicate target rows.

## Phase 3 — automated and focused regression gates

- [ ] Run the backend Django suite against isolated local PostgreSQL:

  ```bash
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_test_db>" \
    .venv/bin/python manage.py test omop_core patient_portal --verbosity=2 --noinput
  ```

- [ ] Run the pytest suite with `DEBUG=True`; ensure `pg_trgm` exists on
  `template1` first, as CI does:

  ```bash
  psql -U postgres -d template1 -c "CREATE EXTENSION IF NOT EXISTS pg_trgm"
  DATABASE_URL="postgresql://postgres@localhost:5432/<release_test_db>" DEBUG=True \
    .venv/bin/python -m pytest -q
  ```

- [ ] Run frontend lint, tests, and build from `frontend/`:

  ```bash
  npm ci
  npm run lint
  npm test -- --run
  npm run build
  ```

- [ ] Run the asynchronous derivation end-to-end suite with a real temporary
  Redis broker, matching CI (`pytest -m e2e -q`).
- [ ] Review all failures, retries, skipped tests, and warnings; do not waive a
  newly failing release-sensitive test without a documented owner and fix plan.

## Phase 4 — release PR and staging validation

- [ ] Push the clean candidate and open/update a PR from
  `release-candidate/1.2-dev-to-main` to `main`. Include the baseline SHAs, the
  Phase 2 evidence, test results, migration ordering, deployment prerequisites,
  rollback plan, and approvers.
- [ ] Require all protected CI checks to pass on the exact candidate SHA.
- [ ] Deploy that exact SHA to staging, where configuration matches production
  (especially Athena source, database engine/version, Redis/Celery, CORS, and
  secret-backed settings).
- [ ] Verify staging startup logs show the intended ordered migration/vocabulary
  sequence and no missing-Athena gate bypass.
- [ ] Smoke-test: authenticated sign-in/access filtering; Patient Record read
  and edit/write paths; Code Mapping review/approval; Therapy Mapping; FHIR
  sync/import; and async derivation. Check error monitoring and worker queues.
- [ ] Capture database migration state, deployment ID, smoke-test evidence, and
  a named release owner approval.

## Phase 5 — production promotion and tag

- [ ] Announce the release window, freeze additional `dev` changes, and
  re-check that `origin/main`, `origin/dev`, and the approved candidate SHA
  still match the PR evidence. If they do not, return to Phase 0.
- [ ] Merge the approved PR to `main` using the repository's normal protected
  merge mechanism. Do not make a separate manual merge that bypasses checks.
- [ ] Confirm the resulting `main` commit is the intended release commit and
  production deployment has started from it.
- [ ] After the merge is visible on `origin/main`, create and push an annotated
  tag from that exact commit:

  ```bash
  git switch main
  git pull --ff-only origin main
  git tag -a v1.2.0 -m "Release 1.2"
  git push origin v1.2.0
  ```

  If the repository requires a signed tag, use `git tag -s` instead. Verify
  with `git show v1.2.0` and record the tagged SHA in the release notes.

## Phase 6 — post-deploy verification and rollback readiness

- [ ] Watch production deploy, error monitoring, Celery worker health, and key
  request/error rates through the agreed observation window.
- [ ] Confirm applied migrations reach `0201`, the Athena load succeeded, and
  the expected HK-Labs mapping counts and target integrity match staging.
- [ ] Run the production smoke tests using non-destructive test data and verify
  backups/snapshots are current.
- [ ] Publish Release 1.2 notes: tag/SHA, highlights, migration/Athena release,
  known issues, validation evidence, and rollback owner.

### Rollback rule

Application rollback may be possible by redeploying the previously known-good
`main` SHA, but migrations and seeded data are not assumed reversible. Do not
run reverse migrations on production during an incident without a reviewed,
database-specific recovery plan and a verified backup. If a migration/data
problem is found, pause traffic or roll back the application as appropriate,
preserve logs and database state, and prepare a forward corrective migration.

## Resume checklist

When returning after an interruption, start here:

1. Read this runbook and `git status --short --branch`; protect any uncommitted
   work before touching the candidate.
2. Re-run the Phase 0 SHA checks. If refs moved, re-establish the candidate and
   repeat the relevant phases.
3. Locate the first unchecked item. The current recorded location is Phase 2,
   before (or while) local PostgreSQL migration validation.
4. Add date, operator, candidate SHA, command output location, and result to
   the release PR/release notes after each phase.
