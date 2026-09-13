# Codex handoff: field and field-value concept mappings

## Resumed work: 2026-09-13

### Additional review and implementation after `57646dc`

**Verified checkpoint `2fe2f6e`:** full pytest **2,356 passed, 4 skipped**;
Django **1,998 tests OK, 1 skipped**; frontend **560 passed, 4 skipped**;
Redis/Celery e2e **2 passed** with an isolated local broker. Production build,
lint (0 errors, 3 existing warnings), migration drift, system and diff checks
passed. All tests used local PostgreSQL, and the joined migration chain applied
to the local application database. This checkpoint is integrated through dev
`07a1cc4`, not the newer changes below. No implementation PR has been merged.

Dev advanced again while these checks ran: `ddbfc6e` (#1258 sample disease
status), `820afd8` (#1218 service identities), and `b8d0ef8` (#1259 cytogenetic
ownership/history) are queued for the next integration. In particular, #1259
makes Genomics the owner of new cytogenetic entry and adds a bounded, authorized
legacy history view. Preserve that contract rather than restoring the old
summary editor. The underlying clear regression tests remain useful for
compatibility and reconciliation.

The next therapy review should validate concept role/validity in the existing
`author_therapy_line` path and verify all-or-nothing behavior when an Episode
cannot be created. The current serializer rejects future start dates; its
managed quarantine paths and all regimen/component/class tables must remain.
Do not create a second therapy catalog or silently invent historical dates.

The next integration includes dev `07a1cc4` (#1249 genomics architecture) and
`bedca25` (#1256 patient-list context). Use upstream's shared component registry,
owned NOTE reader, finding-state logic and provenance implementation; do not
restore the superseded genomics documents or duplicate its text helpers.
Scoped answer resolution and original source aliases are retained. Vocabulary
expiry validation now extends upstream `genomics_vocabulary.resolve_loinc`,
and gene aliases resolve before selecting a dependent answer's gene scope.
Migration `0234_merge_genomics_and_field_values` joins the independent 0231
genomic recipe migration and this feature's 0233 chain without data operations.
Refresh plans also fingerprint vocabulary release provenance records.

At local checkpoint `612d36c`, full pytest passed **2,172 tests (4 skipped)**,
Django **1,998 tests (1 skipped)**, frontend **553 tests (4 skipped)**, build
passed and lint had 0 errors/3 existing warnings. After the latest genomics
integration, **316 integrated backend tests** and **560 frontend tests** passed;
the additional alias/staging/descriptor/reconciliation checks passed **87 tests**.
Full verification of that integration is recorded above.

Local commit `4a68f79` adds the following reviewed fixes and capabilities.
Merge `1e163ed` integrates current dev through `f1bd3a2`, including the scoped
Org Admin permission fix and indexed patient refresh. Conflict resolution
preserves the corrected breast-cancer concepts, vocabulary namespaces, c/p/yp
semantics, source-code fallback and latest-event selection. It also fixes an
automatically merged Oncotype block whose input list had been removed upstream.

- Boolean and numeric JSON identities cannot silently switch (`True == 1` in
  Python is insufficient for immutable canonical values), including transfer.
- Clear resolution follows imported nested question-decision history and
  excludes proposed questions that never had approval.
- Genomics approved answer overrides select their own Measurement/Observation
  destination atomically. Mapped and unmapped aliases preserve source text;
  unsupported assertion roles leave the original record unchanged.
- The existing canonical TNM mapping has a narrow c/p/yp adapter. The basis
  saved in the same PATCH controls the question and qualifier. Pathological
  and post-neoadjuvant pathological results remain distinct; scoped picker
  values come from the existing FieldChoice editor. Ambiguous basis leaves the
  edit pending. Basis is stored context, not a fabricated standalone question.
- Flat-field staging clears suppress older c/p/yp results across current
  Measurement and legacy Observation tables. Same-day un-timed cross-table
  imports cannot override an explicit clear just because their PK is larger.
  New dated facts and later direct corrections remain readable. Linked
  imported assessments are preserved when creating a direct correction.
- `backfill_patient_records` has bounded private signed preview/apply/rollback
  plans for the derived read model. It uses existing refresh, RecordRevision
  and AuditEvent paths, preserves pending clears, rejects drift, and resumes
  per-record application. Rollback refuses later edits and preserves full
  timestamp precision. See [operations and limits](field_value_reconciliation.md).
  This does not implement the separate historical fact-repair modes.

Verification before the latest dev integration: full pytest **2,135 passed,
4 skipped**, Django **1,998 tests OK, 1 skipped**, frontend **553 passed,
4 skipped**, production build passed, lint 0 errors/3 existing warnings.
The new reconciliation suite subsequently passed **10 tests**. The merged
staging/BC/descriptor/reconciliation checks passed **86 tests**; two additional
indexed-source regression tests were added to the merge. Full integrated
backend verification is in progress. No staging clinical writes occurred.

Further review fixes now preserve retired choices during coded-only and alias
readback without making them writable or reviving rejected decisions. Reused
historical aliases stay ambiguous. Genomic question resolution rejects expired
source, relationship and target evidence, and keeps ambiguous Maps-to results
unmapped. Overflow notes require matching patient and variant ownership.
Rollback now reports a missing recovery audit instead of silently counting a
changed record as never applied. Focused retirement/staging/genomics/descriptor/
reconciliation verification: **120 passed**. Full pytest after dev integration,
before these last review fixes: **2,157 passed, 4 skipped**.

Historical answer targets superseded by different decisions still require
provenance-aware reconciliation; retired readback does not infer clinical
correctness from an old rejected mapping. Report metadata/event authoring,
complete disease/catalog dispositions, and other reconciliation modes remain
unfinished.

**Still unfinished. PR #1235 is closed at the user's explicit request because it
was an information handoff. Do not reopen it, merge this branch, or close any
associated issue as part of this checkpoint. The user specifically requires
#21, #26 and #1223–#1231 to remain open.**

Active worktree: `/private/tmp/promop-1235`; branch:
`feat/field-value-concept-mappings`. The original checkout at
`/Users/adamblum/promop` retains its unrelated `AGENTS.md` and frontend lockfile
history; all feature edits were made in the isolated worktree. The original
checkout is now clean after other ongoing repository work. CancerBot at
`/Users/adamblum/cancerbot` was read without modification.

The earlier checkpoint integrated `dev` through `de4c164` (#1234), including #1233 genomics
and #1222 vocabulary checksums. Merge commits `ee39b07` and `a3aa8b2` preserve
upstream changes. Implementation commit `d658a8e` contains the resumed fixes.
The sample-stage conflict was resolved by keeping the dated/coded staging
reader, upstream synthetic-cohort fallback, and blank/Boolean filtering.
R-ISS may supersede an explicitly labeled legacy ISS result; it does not
unconditionally replace a clinical/pathological breast stage.

Implemented and verified since the original checkpoint:

- The confirmed same-day cytogenetic clear regression is fixed. Recursive
  clears preserve `after_pk`; one transaction rolls back all clear writes if
  any destination fails. Tests cover base/override failures, no-op clears,
  retired choices, replaced question history and the original regression.
- Long mapped source aliases use a patient/fact-linked Note instead of silent
  50-character truncation. Retry tests verify note reuse.
- Concurrent/stale choice updates reload under the scope lock, preserving
  another curator's rename, aliases and retirement state.
- Patient pickers show curated display labels and submit canonical numeric
  and Boolean values without converting them to display strings.
- Staging no longer chooses an older linked result over a newer unlinked one.
  Histology chooses by date across Measurement and Observation; default
  scalar readback cannot overwrite the built-in selection with an older row.
  Newer approved question overrides remain readable, and the existing bounded
  snapshot query contract is preserved.
- Migration 0233 now matches vocabulary/code pairs and checks replacement
  validity dates. It withdraws only untouched `hk-labs-seed` Ki-67 source-code
  mappings pointing to ER, recording their old target; explicit reviews are
  preserved for separate reconciliation. No clinical facts are rewritten.
  Definitions rechecked at [LOINC 85337-4](https://loinc.org/85337-4/) and
  [LOINC 29593-1](https://loinc.org/29593-1/).
- Mapping-hub coverage separates field questions from answer choices and
  screens the **existing** regimen/component/class tables. Those tables,
  their relationships, and all existing mapping/management capabilities
  remain authoritative and intact. No replacement catalogs were introduced.
  Existing reference APIs and the therapy editor now expose per-row mapping
  disposition and actual vocabulary/code, including nested relationships,
  unlinked classes, and disease/round associations. Regimen curation loads
  successive 50-row pages; disease/round picker responses remain complete.
  These validity/role screens are not clinical approvals. Existing CRUD and
  relationship-management behavior remains available and tested.
- Candidate search is bounded and resumable, applies a database statement
  timeout per label, and atomically checkpoints each result. Completed
  [candidate evidence](field_value_candidate_search.json): 1,079 distinct
  labels, 342 with candidates, no timeouts. This is name/synonym evidence;
  it does not establish semantic approval or an exhaustive Maps-to review.
- [CancerBot source evidence](cancerbot_field_value_reference.json) records
  commit `a840f8d9af2c35477e2b6b76f981b29f02c21eec`, 172 API option bindings,
  reference-loader literals and 339 checked-in therapy crosswalk rows with
  source hashes. Regenerate with `audit_cancerbot_reference --cancerbot-root
  /path/to/cancerbot --output docs/cancerbot_field_value_reference.json`.
  Crosswalk rows are comparison evidence, never imported approvals.

Render staging reference access works through the main checkout's existing
`STAGING_DATABASE_URL`; credentials were neither copied nor committed. Read-only
queries confirm SNOMED metadata still says `synthetic, benchmark seed`.
**No staging migrations, seeds, patient writes or reconciliations were run.**
All automated tests use local PostgreSQL 18 on port 5433, disposable application
database `promop_1235` and test database `test_promop_1235`.

The CancerBot checkout contains no `.env`, live database configuration or live
ValueOptions export. Source/seed data and the checked-in crosswalk are now
available; live reference parity remains unverified. The user has been asked
where a reference-only export or read-only database configuration is available.
Do not substitute source seed totals for actual live options.

Remaining scope is substantial: complete live inventory and per-value evidence;
full staging assessment/event authoring; report/test metadata linkage; fact/structured and
multi-criterion adapters; exhaustive MM/FL/CLL/genetic/therapy dispositions;
FHIR/source-alias edge cases; and bounded, audited historical reconciliation
with recovery. Passing regression suites does not complete these obligations.
The issue-by-issue table below remains a description of unfinished scope.

Final local verification for this checkpoint:

- Full pytest: **2,115 passed, 4 skipped**, with the two opt-in e2e tests
  deselected by the repository's default configuration.
- Redis/Celery e2e run separately with an isolated broker on port 16385 and
  real local worker subprocesses: **2 passed**. The broker was stopped afterward.
- Full Django `manage.py test omop_core patient_portal --noinput`:
  **1,998 tests, OK, 1 skipped**, including migrations.
- Frontend Vitest with this worktree's own `npm ci` dependencies:
  **552 passed, 4 skipped**. TypeScript/build passed; ESLint reports
  **0 errors and 3 existing warnings**.
- `makemigrations --check --dry-run`: no changes. `manage.py check`: no issues.
  `git diff --check`: clean.
- GitHub state rechecked: PR #1235 **CLOSED**, `mergedAt: null`; all eleven
  associated issues **OPEN**. No new PR, merge, issue closure or deployment.

The sample-stage regression fixture now allocates its imported Observation ID
with the existing sequence helper; factory counters can otherwise collide with
IDs allocated by the backfill command in focused test orders. A readback fixture
without the optional snapshot cache is also supported without additional queries.

---

Original checkpoint: 2026-09-13. **Historical handoff; superseded above where noted.**

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

### 1. Historical clear regression (resolved in resumed work)

The original checkpoint full pytest run (the failure below is now covered and fixed):

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
