# Codex handoff: field and field-value concept mappings

Checkpoint: 2026-09-13. **Work in progress; not ready to merge.**

## Resume instruction

> Continue the work on `feat/field-value-concept-mappings`. Read this document,
> `CLAUDE.md`, and `field_concept_mapping_enhancements.md` first. Fix the known
> cytogenetic-clear regression, review the unfinished implementation, and work
> through issues #1223–#1231 and parent issues #21/#26. Verify the full backend
> and frontend suites, review the PR, and merge only when the intended scope is
> genuinely complete. Do not equate a candidate or `needs_review` row with an
> approved clinical mapping, and do not close unfinished issues.

The user originally requested a plan and implementation issues, then asked to
implement on an independent branch/worktree, create a PR, review and merge.
The latest instruction was to checkpoint and document remaining work for another
terminal/machine. Consequently this is a portable **unfinished** checkpoint,
not a completion or merge claim.

## Repository and working copy

- Repository: `healthkey-ai/promop`; integration branch: `dev`.
- Working branch: `feat/field-value-concept-mappings`.
- Starting commit: `882b36f3ac70f22c5808be623ff0eef00581eb04` from `origin/dev`.
- At handoff, upstream had advanced to `12acf900d7b529cac6840f451de7495d28ff1b9b`.
  It includes genomics PR #1233 (issue #1232, commit `9dacc7b`) and vocabulary
  checksums PR #1222. **The genomics change overlaps `services/genomics.py`.**
  These commits are not integrated into this checkpoint; reconcile them on resume.
  No new upstream migration files were detected in that comparison.
- Original worktree: `/private/tmp/promop-field-value-mappings`.
- Original main checkout: `/Users/adam/promop`; its unrelated work was not changed.
- Another user worktree, `/private/tmp/promop-genomics-complete`, was active.
  Do not reset, delete or overwrite its work. Recheck upstream genomics changes
  and migration numbering before rebasing this branch.
- No staging migrations, seed writes, patient changes, or reconciliation runs
  have been applied by this session. Staging was used read-only for reference data.

On another machine with the repository and GitHub access:

```sh
git fetch origin
git worktree add -b feat/field-value-concept-mappings ../promop-field-values origin/feat/field-value-concept-mappings
cd ../promop-field-values
```

If that local branch already exists, omit `-b` and use the existing branch.
Do not recreate the implementation from this document: all source changes are
in the checkpoint branch. Read the actual diff before extending it.

## Scope and issue status

The requested plan is committed at the repository root:
[`field_concept_mapping_enhancements.md`](../field_concept_mapping_enhancements.md).
It is the detailed clinical/architectural specification, not a completion report.

| Issue | Checkpoint coverage | Remaining |
|---|---|---|
| [#1223](https://github.com/healthkey-ai/promop/issues/1223) inventory/evidence | Reference inventory command and staging artifact; source-only CancerBot scan; candidate search command | Finish candidate search, integrate field/option evidence, obtain live CancerBot database-generated options, reconcile totals and dispositions |
| [#1224](https://github.com/healthkey-ai/promop/issues/1224) scoped identity/reviews | Schema, migration, validation, transactional review service, revisions, typed canonical values and aliases | Review concurrency/immutability/migration tests, context recipe design, compatibility issues below |
| [#1225](https://github.com/healthkey-ai/promop/issues/1225) curation editor/API | Review endpoint, search/review/history UI, typed/contextual choice creation, rename/aliases | Finish permission/search/retirement/context tests and verify downstream pickers use canonical values with display labels |
| [#1226](https://github.com/healthkey-ai/promop/issues/1226) projection/readback/FHIR | Scalar coded answers, reverse resolver, question overrides, partial genomics integration | Fix clear regression and atomicity, context-aware writes, fact/structured adapters, FHIR regression coverage, long-source preservation |
| [#1227](https://github.com/healthkey-ai/promop/issues/1227) breast cancer/#21 | Several incorrect codes repaired, dated c/p/yp reads, HRD/coded histology, unknown semantics, withdrawal migration | Complete event/context-aware writes and report linkage; review all #21 acceptance criteria; reconcile old erroneous facts safely |
| [#1228](https://github.com/healthkey-ai/promop/issues/1228) MM/FL/CLL/shared | Some field candidates and lookup options can be proposed | Most disease-specific semantics/structured representations and exhaustive curation remain |
| [#1229](https://github.com/healthkey-ai/promop/issues/1229) genetics values | Reuses existing genomics catalog; origin/interpretation proposals; linked component read/write integration | Full gene/variant/dependent-option mapping, ambiguity/context handling, complete tests and source reconciliation |
| [#1230](https://github.com/healthkey-ai/promop/issues/1230) therapies | Existing regimen/component/class rows and relationships included in inventory | Validate actual concept meanings/domains, complete missing candidates and picker/adaptor coverage; do not create another therapy catalog |
| [#1231](https://github.com/healthkey-ai/promop/issues/1231) transfer/reconciliation | Natural-key transfer, concept re-resolution, imported history, dry-run seed, `--field-and-values` | Bounded reconciliation/rollout/rollback tooling, transfer edge cases and operating documentation |

Parent [#21](https://github.com/healthkey-ai/promop/issues/21) and
[#26](https://github.com/healthkey-ai/promop/issues/26) are **not complete**.
No implementation issue was closed during this session.

## Implemented files and design

- `omop_core/models.py`: extends `FieldChoice` with `code`, `canonical_value`,
  `aliases`, `context_key`, `retired`; adds `FieldValueConceptMapping` and
  `FieldValueMappingRevision` with protected references.
- Migrations `0231`/`0232`: schema and legacy identity/proposal migration.
  Existing primary codes become proposals, never automatically approved answers.
- Migration `0233_withdraw_invalid_bc_field_recipes`: withdraws a narrowly
  identified set of incorrect question/analyte/answer recipes. Prior decisions
  are recorded in notes. Corrected questions are proposals, not approvals.
  It does not repair historical clinical facts. Review this migration carefully.
- `services/field_values.py`: current-standard/domain checks; alias collision
  validation; advisory scope lock; reviewed decisions and audit history;
  request/snapshot-scoped resolver. One bounded joined value query is shared by
  the extractors through the snapshot cache, not a process-global cache.
- `patient_portal/api/field_values.py` and URL registration:
  `GET/PATCH /api/v1/field-choices/{id}/mapping/`, using existing mapper-admin
  authorization. API choice identity is immutable; display changes retain aliases.
  Deleting a choice with a mapping retires it instead of deleting its history.
- `FieldChoiceEditor.tsx` / `ValueMappingEditor.tsx`: concept search separates
  question and answer roles; explicit dispositions, evidence/release/status,
  history, typed/contextual creation, display/alias edits. The existing compact
  mapper design was retained; this is not a replacement mapping application.
- `services/omop_projection.py`: approved scalar answer emission and reverse
  readback; raw-value fallback; question overrides and clear handling.
  **Whole-fact role currently returns false/pending in the generic writer.**
  A reviewed fact mapping is not yet an implemented general occurrence adapter.
- `services/genomics.py`: integrates the resolver into existing linked variant
  components using `genetic_mutations.{key}`. Do not recreate variant storage.
- `services/breast_cancer.py` and `patient_record_service.py`: dated staging,
  exact question/source matching, selected biomarker corrections and three-valued
  unknown handling. Several breast-cancer obligations remain below.
- `services/field_curation_transfer.py`: extends existing choices transfer with
  stable identity, scoped natural keys, mappings/history and target re-resolution.
  Existing transfer defaults remain `mappings,synonyms`;
  `copy_curation --field-and-values` explicitly includes choices/custom fields.
- `services/field_value_seeds.py` and `seed_field_value_mappings`: versioned
  candidate questions/answers plus unresolved existing lookup options. Dry-run
  by default. No clinical approvals are generated.
- `audit_field_value_mappings` and `search_field_value_candidates`: reference-only
  inventory and unapproved name/synonym candidate evidence.
- Generators, crossmap/HK-labs seed commands and provenance definitions contain
  selected code corrections. Historical migrations were not generally rewritten.

## Known failures and review priorities

### 1. Fix the confirmed clear regression first

Latest full pytest run:

```text
FAILED tests/test_cytogenetic_observations.py::test_same_day_legacy_import_then_clear_does_not_restore_marker
assert refresh_patient_record(record.person).cytogenetic_markers == ''
actual: 't(4;14)'
1 failed, 1959 passed, 4 skipped, 2 deselected
```

Likely cause: the new recursive clear path in `project_single_value()` does not
forward `after_pk` to its recursive base call. The existing cytogenetics adapter
uses that boundary to create a clear newer than a same-day aggregate import.
Confirm this against the test; preserve its original safety assertion.

Also review the new clear path's `changed or base` result: a successful override
must not acknowledge a failed base write (or vice versa). Existing exception
handling returns false; nested transactions alone do not make partial success
safe. Test no-op clears, partial failures, override changes, and withdrawn/retired
mapping history. Merely propagating `after_pk` does not complete this review.

### 2. Core integration still needs review

- The general writer supplies default context unless `projection.context_key`
  is provided. The UI can create scoped choices, but disease/staging/method context
  is not automatically routed through all runtime paths. In particular **c/p/yp
  reads do not imply implemented c/p/yp writes**.
- Main field mappings and value overrides must stay compatible. Test changing
  questions, choosing a different override, source-only imports and reverse
  ambiguity when several choices share a target.
- Existing cytogenetic per-choice fact writes remain authoritative. Its descriptor
  still reads legacy `FieldChoiceCode` and should be reviewed for retired choices,
  canonical renames and rejected/new value decisions. Do not break this existing
  multi-select adapter while adding generic mappings.
- Full-fact/structured roles currently record decisions but are not general
  event-aware writers. Do not claim multi-value/occurrence projection is complete.
- Audit raw source preservation: `value_source_value` is limited to 50 characters;
  canonicalization must not silently lose longer source text or structured input.
- API validation handles identity/alias rules; direct ORM saves/bulk updates can
  bypass some validation. Consider the intended service boundary and add direct
  migration/concurrency tests. Review stale-instance updates under scope locks.
- Test environment transfer with old payloads, unresolved/different concepts,
  invalid approvals, retirement, changed aliases, idempotence, imported reviewer
  provenance and dry-run rollback. Current `read_payload` expects the new schema
  on its source; the audit command alone supports pre-migration staging schema.
- New `FieldChoice` descriptor options now use typed canonical values and optional
  `label`; verify `ClinicalField` and other consumers do not discard those labels.

### 3. Breast-cancer repair is partial

- Complete linked test/report metadata; current scalar fallback can still combine
  independently dated methodology/specimen/interpretation facts. Do not present
  unrelated report data as one test event.
- Oncotype invasive and DCIS questions are distinct. The seed proposes invasive
  NAACCR 3904; runtime/test context must distinguish 3903 rather than guessing.
- PD-L1 assay clones are not TPS/CPS scoring-method answers. `105302-4` names the
  scoring method, not the antibody clone. Assay curation remains unresolved.
- `staging_data()` currently chooses a linked assessment if any candidate is
  linked; review whether an older linked row can displace a newer unlinked row.
  Its legacy R-ISS fallback can override overall `stage`; disease context needs
  review. Preserve tumor, date, staging system/edition and c/p/yp distinctions.
- Bone-only metastasis cannot be inferred from a generic bone-site/metastasis
  assertion. Incorrect broad extraction was removed, not replaced by a validated
  longitudinal site-based computation.
- Explicit HR versus computed HR provenance/precedence needs tests for edits,
  clears and refreshes. Unknown/equivocal must not become negative TNBC.
- Search for other bad recipes/import aliases beyond the edited files. Existing
  **source-code mappings** seeded by migration `0201` can still map Ki-67 to ER;
  updating the generator/seeding command does not correct existing SCCM rows.
- Review migration `0233` against the inventory, and add focused forward/idempotent/
  curator-preservation tests. It must not silently invent new approvals.

### 4. Curation and rollout are not done

Some of the explicit unresolved meanings in the plan require structured facts,
not a single scalar concept: MM bone-lesion counts, GELF criteria, transplant
eligibility versus a completed procedure, response/MRD distinctions, contextual
stages and grades, marker polarity, exact variants and therapy status/rounds.

Reuse existing therapy, mutation, episode and cytogenetic capabilities. Do not
build the originally proposed duplicate lookup-model system. Do not mark
`no_equivalent` merely because a short lexical search found nothing.

No historical patient reconciliation command was added or run. Design dry-run,
bounded batches, audit reporting, source preservation, pending-edit protection,
rollback and post-run coverage before attempting it. Never reinterpret old ER
facts as Ki-67 just because an earlier importer wrote an incorrect label.

## Athena evidence and inventory

[`field_value_mapping_inventory.json`](field_value_mapping_inventory.json)
contains the reference-only staging snapshot:

- 414 field/mapping paths; 314 have existing field mapping rows.
- 262 `FieldChoice` rows.
- 977 reference options across lookup and therapy catalogs.
- Existing regimen/component/class links, full local genomics catalog and a
  non-executing AST scan of CancerBot literal option lists.
- CancerBot source checkout used: `d23b5cb3cd351f9bf2ec9877c2e4146c1c5ce930`.

The latest published staging vocabulary release was ID 11,
`Athena v5.0 29-AUG-26`, published 2026-08-31. The separate `vocab_release` table
was empty. SNOMED metadata said `synthetic, benchmark seed`; this is a provenance
warning, even where concept IDs look external and `standard_concept='S'`.
See the plan for verified concept IDs, question/answer distinctions and all
clinical candidate tables.

The name/synonym search was started but stopped for the machine handoff before
completion. **No complete candidate-search artifact is claimed.** The command
currently accumulates output until the end; consider checkpointing/batching and
bounded query timeouts before a long rerun. A ForeignKey `startswith` query bug
was fixed to use `vocabulary__vocabulary_id__startswith` before this attempt.
The stopped query was the exact-synonym `Concept` subquery; cancellation reported
a timeout before the client exited with KeyboardInterrupt. Enforce a bounded
`SET LOCAL statement_timeout` in the command itself and verify connection options
rather than assuming `PGOPTIONS` wins over Django settings. The local process
was stopped; no search client needs to be resumed on the original machine.

A live **reference-only CancerBot ValueOptions JSON export** is still needed to
prove parity for database-generated lists. The user was asked asynchronously;
no export was available at this checkpoint. A source scan cannot establish live
options. A zero/unmapped candidate count is not a reason to omit an option.

## Validation state

| Check | Last observed result |
|---|---|
| Focused backend: value mappings, BC semantics, transfer, descriptor, edit history | 141 passed before the most recent UI/migration changes |
| Latest full pytest | 1959 passed; 1 failed (cytogenetic clear above); 4 skipped; 2 deselected |
| Full Django suite, freshly created test DB | 1993 tests; 2 failures, 1 error, 1 skip, before subsequent fixes |
| Full frontend tests | 546 passed; 4 skipped |
| Frontend production build | Passed before latest identity-editor changes; rerun |
| `makemigrations --check --dry-run` | No changes detected before addition of data-only migration 0233 |
| `git diff --check` | Passed |

The three fresh-Django failures were:

1. Protected value mappings prevented a **legacy test simulation** from deleting
   every Concept. The simulation fixture cleanup now removes value revisions and
   mappings first. The production vocabulary loader already avoids TRUNCATE;
   do not weaken the new PROTECT relationships.
2. Nottingham numeric grade rejected database Decimal formatting. Conversion
   was repaired to accept exactly numeric 1/2/3 (not booleans).
3. Refresh used 22 queries rather than 21. The shared answer lookup accounts for
   one new bounded query; the documented budget assertion is now 22.

Those patches have **not** had a complete fresh-Django rerun. Also, an intermediate
`--keepdb` rerun produced many misleading reference-fixture failures after test
flushes. Use a freshly created isolated test DB for authoritative full-suite runs.

Local logs existed only on the original machine under `/private/tmp/`:
`promop-field-values-pytest.log`, `promop-field-values-django-fresh.log`, and an
older misleading `promop-field-values-django.log`. Their meaningful results are
recorded above; the remote branch does not depend on those logs.

## Setup and verification on the new machine

Read `CLAUDE.md` for current project instructions. This session used Python 3.12,
PostgreSQL 18 with pgvector on port 5433, and Node dependencies from the main
checkout. The venv and `frontend/node_modules` symlink are **not portable** and
are not committed. Install the dependencies normally on the new machine.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd frontend
npm ci
cd ..
```

Create a dedicated empty LOCAL database using your local PostgreSQL credentials.
The original disposable application database was `promop_field_values`; its test
database was `test_promop_field_values`. Never point tests at staging.

```sh
export DEBUG=True
export DATABASE_URL=postgresql://postgres@localhost:5433/promop_field_values
.venv/bin/python manage.py migrate
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run
.venv/bin/python -m pytest tests/test_cytogenetic_observations.py tests/test_field_value_mappings.py tests/test_breast_cancer_semantics.py -q
.venv/bin/python -m pytest -q
.venv/bin/python manage.py test omop_core patient_portal --noinput
cd frontend
npm test
npm run build
```

Do not run Django/pytest concurrently against the same disposable test DB.
The original LOCAL application DB had migrations through 0232 applied; 0233
was added later and was not explicitly applied there.

For an authorized reference audit, configure a separate process with the user's
read-only staging connection supplied through the environment. Do not commit
credentials or copy another machine's `.env` wholesale. The original temporary
runner used `PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=45000'`.
The audit/search commands also start read-only transactions.

```sh
# Only in this separate read-only reference-audit process:
DATABASE_URL="$STAGING_DATABASE_URL" PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=45000' \
  .venv/bin/python manage.py audit_field_value_mappings \
  --output docs/field_value_mapping_inventory.json \
  --cancerbot-source /path/to/cancerbot/trials/services/value_options.py

DATABASE_URL="$STAGING_DATABASE_URL" PGOPTIONS='-c default_transaction_read_only=on -c statement_timeout=45000' \
  .venv/bin/python manage.py search_field_value_candidates \
  --inventory docs/field_value_mapping_inventory.json \
  --output docs/field_value_candidate_search.json
```

Use `--cancerbot-export /path/to/reference-only-value-options.json` when available.
Do not use a patient export. Seed command `seed_field_value_mappings` is dry-run
by default; `--apply` writes proposals to its configured database. Test locally
and review its dispositions before authorizing any shared-environment run.

## Completion sequence

1. Inspect the checkpoint diff and current upstream; preserve other work.
2. Fix the confirmed regression and review clear/alias/context safety.
3. Finish data inventory/curation and the remaining issue-specific adapters.
4. Complete reconciliation and safe rollout instructions; no blanket remapping.
5. Run fresh full backend/frontend tests; review migrations, permissions and
   clinical semantics. Update this handoff and each issue with accurate outcomes.
6. Mark the draft PR ready only when its stated scope is complete; review and
   merge to `dev` as requested. Do not auto-close parent #21/#26 prematurely.
7. Run required post-merge verification. Clean up only this feature worktree and
   branch when safely merged, not another user's checkout/worktree.
