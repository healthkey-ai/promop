# Provisional data plan

Future work: OMOP rows and `PatientRecord` values written from **proposed**
source-code mappings, marked with the confidence the mapping was proposed at,
so a patient's page can show what the data probably says before a curator has
confirmed it. Tracking issue:
[#2434](https://github.com/healthkey-ai/promop/issues/2434). Prepared
2026-11-10 against `dev` `71a44790`, revised 2026-11-12 to keep `patient_record` unchanged
and put provisional data in a parallel pair of tables. This document
specifies the work; nothing here is implemented yet. The write architecture it
builds on is [PatientRecord-first writes](patient-record-first-writes.md) and
the resolution rules in `omop_core/mapping/code_resolution.py`.

## Problem

Ingest never acts on a proposed mapping. `resolve_source_code` returns no
concept for a code whose only mapping is `proposed`, so the fact is stored at
concept 0 (FHIR sync, the upload endpoint), left out of the clinical tables
altogether (the ETL, which writes only what `code-mappings/lookup/` resolves),
or never created from the per-patient inventory (Generate OMOP creates rows
only for approved mappings). The patient page shows nothing for the field.

On HealthTree most hospital codes are proposed, not approved: Suggest has
scored them, often at 80-95% confidence, but a curator has not reached them.
Every one of those codes is a lab result or a diagnosis the patient's page
cannot show, and a backlog of thousands of codes means the page stays empty
for months while the answer sits in the queue.

## Definition

A **provisional** fact is an OMOP clinical row, or a patient-level value,
whose destination concept came from a proposed mapping rather than an approved
one. It is marked by a **confidence**: the ranker's confidence in that
proposal, as a whole-number percentage (0-100). The percentage is not the 0-1
float Suggest records on `SourceCodeConceptMapping.suggestion_confidence`: a
two-digit number is what the page shows and what a threshold is set in
(#1532), and an integer compares exactly. A proposal at 100 is still a
proposal.

The design keeps `patient_record` exactly as it is: a flat table of approved
data, read by everything that reads it today. Provisional data lives in a
**parallel pair of tables with the same column names**, so a consumer chooses
the table, not a column convention.

| Table | Holds | Null means | Read by |
|---|---|---|---|
| `patient_record` (unchanged) | approved data only | no approved answer | every existing consumer, with no change |
| `patient_record_provisional` | the best-known picture: the approved value where there is one, the proposal's value where only a proposal answers | no answer of either kind | a foundation or page that wants provisional data |
| `patient_record_confidence` | one percentage per field, under the same column name | the value in the provisional table is approved (or absent) | the badge, and any reader that must know which values to trust |
| `measurement`, `observation`, `condition_occurrence`, `drug_exposure`, `procedure_occurrence` | one new nullable `confidence` column | a real row: its concept came from an approved mapping, a direct identity, or a user's own edit | derivation, the OMOP tab, the lab APIs |

The `confidence` column on the five clinical tables stays on the row, not in a
parallel table: it is what approval clears and rejection reverts, and a
second table there would double every join on the largest tables.

Three invariants hold everywhere:

1. **Real beats provisional.** A field with an approved answer takes it; a
   provisional row answers a field only when no approved row does. A user's
   edit (`user_edited_fields`) is approved.
2. **Provisional rows follow their proposal.** Approving the proposal clears
   the confidence (the rows become real). Rejecting it, or re-pointing it
   elsewhere, takes the rows back to concept 0 with no confidence: exactly the
   unresolved row ingest would have written without this feature.
3. **`patient_record` never contains a provisional value.** Its derivation
   reads only rows with a null `confidence`. Every consumer that reads it
   today, including the legacy `patient-info` path, the federation host,
   PRism and FHIR export, is safe without being changed; the ones that read
   the clinical tables directly are the review list below.

## What is not provisional

- **A user edit.** `project_single_value` writes a real row, and the edit is
  mirrored into the provisional table with its confidence cleared. A patient
  or clinician typing a value is a fact, whatever the mapping queue says.
- **A direct identity.** LOINC and SNOMED codes, and Athena `Maps to` targets,
  resolve without a mapping and are real (rules 2 and 3a in `code_resolution`).
- **A quarantine placeholder.** An import-minted `HK-*` concept is not a
  destination a provisional row may carry: `_usable_destination` refuses it as
  it does for the promotion path. A proposal whose target is a placeholder
  writes what it writes today.
- **A proposal with no confidence.** An import proposal that nothing ranked
  (`suggestion_confidence` null) carries no number, so no provisional row can
  be written from it. Only a ranked proposal qualifies.
- **A proposal below the floor.** `PROVISIONAL_CONFIDENCE_FLOOR` (default 70,
  the threshold #1532 proposes for auto-approval) is the least confidence a
  provisional row is written at. Below it the code stays unresolved as today.

## Where provisional rows come from

There are four writers of clinical rows from source codes. Each gets an
explicit opt-in; none changes behaviour by default.

| Writer | Today | With provisional data |
|---|---|---|
| Generate OMOP (`_create_missing_clinical_rows`, `resolve_person_source_codes`) | creates and re-points rows for approved mappings only | also creates a row for a ranked proposal at or above the floor, with its confidence; re-resolve promotes or reverts provisional rows as the proposal moved |
| `code-mappings/lookup/` (the ETL) | an unresolved entry carries `proposed_target_concept_id` as curator evidence, which the ETL must not write | with `include_provisional: true` in the request, such an entry also carries `provisional: true` and `confidence`; the ETL writes the row with that `confidence` through the bulk endpoints |
| Bulk OMOP writes (`POST /api/v1/measurements/` etc.) | no `confidence` field | accept `confidence` on a row; refuse it on a row at concept 0 and refuse it from a session user (machine credentials and staff only) |
| FHIR sync and `upload_fhir_bundle` (`resolve_source_code`) | a proposed mapping returns no concept | a new `accept_proposed=True` argument returns the proposal's concept and confidence; sync passes it when the organization or deployment opts in |

`resolve_source_code_variant` already has a `provisional` keyword that means
something else (one lookup sighting per batch, #2123). The new argument is
`accept_proposed`, and the docstring says why the two are different.

Generate OMOP is the first writer to build, because it is the one HealthTree
needs: the per-patient inventory (`PatientSourceCode`) is loaded, the
proposals are ranked, and a curator can run it from the patient page today.
The lookup contract is second: the ETL repository has to change with it. FHIR
sync is third and is gated per organization.

### As built in #2662: the server makes the rows

The ETL does not propose concepts. Proposals come from Code to Concept and
Concept to Code, each stored with a confidence on the mapping
(`suggestion_confidence`). An ETL load leaves the rows for such a code at concept
0, so the ETL needs no change and the lookup/`confidence` contract above is not
on HealthTree's path: the server resolves them with the per-person resolver that
Generate OMOP already uses (`resolve_person_source_codes`), and
`omop_core/services/provisional_sweep.py` decides for whom and when. Off with
`PROVISIONAL_DATA_ENABLED`: nothing is queried, queued or written.

- **One patient at a time.** `manage.py fill_provisional_record <person_id>`
  (#2696) proposes with Suggest (Jev by default) for the codes the patient has
  unresolved rows under (`--include-inventory` adds the slow inventory-only ones),
  never for a row under a code system the patient does not hold; resolves **every**
  row against **every** mapping with the full per-person resolve (approved makes
  rows real and creates the rows an inventory code lacks, a proposal at or above
  the floor makes them provisional, below it leaves them); and rederives their
  record inline, `patient_record` from approved rows and the provisional pair from
  proposed ones; see AGENTS.md. It never writes a code the ranker could not rank
  (the run stops, exits non-zero, and a rerun retries it), and `--tables` and
  `--limit` narrow only the propose step. It does not override the floor: the
  report says how many of their proposals fell below it, by band of the real floor,
  so the floor can be a product decision rather than a per-run flag a later sweep
  would undo.
- **Sweep.** `manage.py sweep_provisional_rows` (also queued on the worker, and
  `--dry-run`, `--mapping-id`, `--after-person-id`) resolves, in person-id chunks
  of 200, every person holding an unresolved row, a provisional row or an
  inventory code with no row under a qualifying proposal (unscoped, `proposed`, a
  destination, a confidence at or above the floor); and every holder of any
  provisional row, so a proposal that stopped qualifying with no hook to say so
  gets its rows taken back. Each person's resolve is limited to the codes that
  can change (`person_scope`): the person's own spellings of a qualifying code
  (matched case-insensitively, as the resolver matches a proposal), as rows or
  inventory, plus the codes of rows already provisional. Unrestricted it re-walked
  the whole inventory (thousands of resolved codes, about 5 minutes a person on
  HealthTree, #2663); restricted it costs one query per clinical table plus one
  for the inventory, whatever the size of the chart or of the qualifying set. The
  full sweep therefore no longer re-resolves rows of codes under an approved
  mapping that carry no provisional mark; Generate OMOP and the per-patient
  resolve own those. Idempotent (a person already provisional at the
  current proposal is "already resolved"), never touches a real row, and the
  counts land on a `SourceCodeResolveRun`. It is resumed by its cursor, the last
  person id a chunk finished, which travels in the Celery task arguments (a
  killed task retries its own chunk) and is printed by the command; no cursor is
  stored, because the selection shrinks as persons are resolved and the cursor
  covers the ones that stay candidates (a provisional row is rewritten, never
  done). A retried chunk can double-count in the run's totals; the rows do not
  change.

  **A run's state.** The run is `running` from its first chunk and `completed` (with
  `finished_at`) when the chunk that reaches the end of the candidates finishes;
  a full sweep does not know its total in advance, so `total` is the persons done
  so far, and a poll showing `running` with `done` equal to `total` is a sweep
  between or inside chunks, not a finished one. Every progress save and state
  change stamps `updated_at`, the run's heartbeat. A run whose worker was lost
  (a deploy restarts the worker pool and a chunk in flight can be lost with it)
  stops moving and stays `running`: `manage.py sweep_provisional_rows
  --close-stale [--stale-minutes N] [--dry-run]` closes, as `failed`, every run
  still pending or running with no heartbeat for N minutes (default 120, well
  past a chunk's time budget and the broker's redelivery window). It changes run
  rows only, needs no switch, and covers the bulk resolve too (they share the
  table). A chunk that does arrive later reopens its run and finishes it. A sweep
  run from the command that is interrupted or crashes is marked `failed`, since
  no task is left to retry it.
- **Trigger.** A `pre_save`/`post_save` pair on `SourceCodeConceptMapping`
  compares what the mapping said before the save with what it says after (does it
  qualify, its destination, its confidence, its table) and queues a sweep of its
  code, after commit, when it qualifies now or did before **and** the answer
  changed. A new or changed proposal reaches patients; a withdrawn one (rejected,
  destination cleared, confidence below the floor) is taken back; a save that only
  changed notes, or a proposal with no ranker confidence, queues nothing. Every
  writer that calls `.save()` is covered (Suggest, group Suggest, reverse
  Suggest, suggest-one accept, Concept to Code, the dialog and Calc); a
  `.update()`/`bulk_update()` is not, and the sweep covers it. Approval and
  rejection are unchanged (#2530): approval promotes, rejection or a moved
  destination reverts.
- **Only with a broker, never in a request.** A trigger or the ingest hook below
  resolves nothing (and so derives no record) inside a request. With no
  `CELERY_BROKER_URL` they do nothing (logged at debug) and
  `manage.py sweep_provisional_rows` is the way to run it, which also makes it
  the path on a developer machine.
- **Coalesced.** One pending sweep per code and one pending job per person: a
  cache key is taken when the job is queued and dropped when it starts, so a
  batch of saves for one code, or twenty ingest POSTs for one patient, queue one
  job, and a change after the job has started queues another. The key is taken
  inside the commit callback, so a save that rolls back leaves none. The cache is
  best effort: an outage means a duplicate job, never a failed save or POST, and a
  per-process cache (no `CACHE_URL`) is warned about once because coalescing is
  then not shared between workers.
- **After ingest.** The bulk OMOP write and the FHIR sync call
  `queue_person_resolve(person_id)` once per request: a cheap check that this
  person would change (an unresolved row, an inventory code with no row, or a
  provisional row that no longer matches its code's proposal), and only then a
  resolve job after commit. The check is one query per clinical table for the
  person's own distinct (code, concept, confidence, erroneous) and one for their
  inventory, compared in Python against a cached index of the qualifying
  proposals with the resolver's own choice rule (`choose_proposal`), per code
  system and case-insensitively, so a Cerner-coded person with only an Epic
  proposal is not queued. A request never builds the index (it loads every
  proposal): a cold cache queues a warm-up in the worker and the check says "not
  now"; every sweep chunk refreshes it, which is how a new proposal reaches the
  check (up to an hour stale otherwise). It counts a write that only **updated**
  rows too: an ETL replay re-posts a row at concept 0 and the upsert rewrites the
  provisional row to unresolved, and the queued resolve makes it provisional
  again (tested end to end). New patients do not wait for a sweep; with the
  switch off, or no broker, it costs no query.
- **Safe to run concurrently and for long.** A chunk stops on a 600 s budget and
  hands its cursor on, saving progress every ten persons; both tasks carry a soft
  time limit (60 s under `CELERY_TASK_TIME_LIMIT`) so the chunk can save progress
  and return its cursor before the hard limit, and a sweep whose retries are
  spent marks its run FAILED, as does an enqueue that fails. The per-person
  resolve holds a Postgres **session** advisory lock on the person id (bounded
  wait; a person another worker holds is skipped by the sweep and retried by the
  job) while the resolver runs its own short per-group transactions. It is
  deliberately not one outer transaction: that held row locks across code groups
  and deadlocked with the ETL's keyed bulk write (tested with two threads), and
  ran an inline derivation inside the lock's transaction so a timeout rolled the
  whole resolve back. Each group's rows commit before the next, and the
  derivation runs after them; a derivation failure surfaces as an error on the
  run but never undoes the rows.
- **Never made provisional:** a row entered in error. Taking a provisional row
  back still applies to one.
- **Only unscoped proposals qualify.** As `provisional_proposal` already
  required: the per-patient inventory cannot say which hospital a code came from,
  so **an organization-scoped proposal writes no provisional rows**. A mapping is
  scoped only when the caller passes an organization for a code system such as
  Epic or Cerner; staging has none, but HealthTree's ETL may pass
  `organization_id`, which must be confirmed before enabling on HealthTree.
  Candidate selection matches the source code exactly (the resolver then matches
  case-insensitively for the persons it finds). Not guarded: a code whose
  proposal under one code system was rejected can still fall back to a proposal
  for the same code under another system, as the resolver already behaves.

## Approval, rejection and a moved proposal

`repoint_clinical_rows` is the one place rows change concept after the fact,
and it matches rows on the mapping's source value **and** the old destination.
Provisional rows sit at the proposal's concept, so three cases extend it:

- **Approved at the same concept.** No re-point is needed. The approval path
  clears `confidence` on rows matching the source value at that concept, in
  the same statement that marks the records stale.
- **Approved at another concept.** The re-point moves rows from the old
  destination to the new one as today, and also moves the provisional rows at
  the proposed concept (added to the old-destination set, as
  `deferred_repoint_concept_ids` already carries concepts a patient-scoped
  approval left behind) and clears their confidence.
- **Rejected, or the proposal re-pointed by Suggest or a curator.** Provisional
  rows at the previous proposed concept go to concept 0 with confidence
  cleared (`revert_provisional_rows`, in the same module). Rows ingest wrote
  at concept 0 in the first place are untouched. A later Suggest run that
  proposes a new concept writes new provisional rows only when Generate OMOP or
  the ETL runs again: a proposal moving does not itself write rows.

Every one of those marks the patients stale (`derivation_version=0`) and
leaves the derivation to `backfill_patient_records`, as the re-point does.
Collapse of duplicates (`_collapse_duplicates`) treats `confidence` as part of
the identity: a real row and a provisional row for the same event are two
rows until the provisional one is promoted, at which point the next collapse
folds them. On the bulk write path (`_plan_bulk_upsert`), a proposal that
matches a real row for the same content key folds into that real row: the real
row survives and the proposal leaves no row (#2543). The FHIR sync keeps the
same survivor and, when it rewrites a provisional row, clears its confidence: a
sync is a real EHR write, so the row stops being a proposal when the sync
resolves a concept; an unresolved code keeps it provisional (#2581).

Unit normalization on approval (`normalize_measurement_units`) runs on
promoted rows as on re-pointed ones. Provisional rows are **not** normalized
at write time: the proposal has no curated `destination_unit_concept`, and a
write-time conversion is an approved mapping's privilege (#2157).

### As built in phase 0b (#2528)

- **The two tables are generated, not typed.** `PatientRecordProvisional`
  clones every `PatientRecord` column when the models load
  (`_provisional_columns`), except the key, the lifecycle columns that describe
  the real row (`derivation_version`, `derived_at`, `created_at`, `updated_at`)
  and `user_edited_fields`, which stays one source of truth
  (`PROVISIONAL_EXCLUDED_COLUMNS`). A clone drops `unique` and `db_index`;
  `organization` is rebuilt by hand because a foreign key cannot be
  deconstructed while models load, and keeps its own index for tenancy.
  `PatientRecordConfidence` has one nullable `PositiveSmallIntegerField` per
  non-JSON column in `_OMOP_DERIVED_FIELDS`: 274 at first, plus the 46 JSON
  lists of `PROVISIONAL_JSON_FIELDS` (#2534, migration 0360). A field added to
  `PatientRecord` therefore needs the same migration to add its twin, which
  `makemigrations` writes by itself.
- **`_OMOP_DERIVED_FIELDS` moved** to `omop_core/derived_fields.py`, plain data,
  because a model module cannot import `patient_record_service`.
  `patient_record_service._OMOP_DERIVED_FIELDS` is the same list object.
- **Full, not sparse.** One row per person. The HealthTree row count that would
  settle it could not be read (the Cloud SQL proxy's credentials had expired),
  so this is the plan's default; sparse is a change to the provisional pass and
  the endpoint, not to the schema, and can still be made once the count is
  known.
- **Classification.** Both models are patient data in `instance_data.PATIENT` and
  also in `PATIENT_DERIVED`: `copy_patient` does not copy them, the refresh it
  runs afterwards rebuilds them. A person's delete cascades.
- Tests: `tests/test_patient_record_provisional_tables.py`.

### As built in phase 1c (#2531)

All of it behind `PROVISIONAL_DATA_ENABLED`. Code: `omop_core/services/provisional_record.py`.

- **The pass** runs at the end of `refresh_patient_record`, in its transaction,
  once the real record is saved. Failures are logged and leave the provisional
  pair as it was; the approved record is never lost to it. The pair is written
  for a person who has provisional clinical rows, or already has a stored pair
  (so an approval or a revert rewrites it clean); the provisional row is a copy
  of the real record with the overlay, the confidence row all null unless a
  value is provisional. A person who never had a proposal has no row, which the
  endpoint answers with the real record. That is sparse by use: it keeps the
  common refresh free of two 400-column upserts, and makes the HealthTree row
  count a smaller question than the plan assumed.
- **Rollout:** the pair is not written, and the PATCH mirror does not run, while
  the switch is off, so rows written earlier go stale. After enabling it, run
  `backfill_patient_records --all` before relying on the endpoint.
- **Readers run:** labs and blood counts (`_get_laboratory_data`), assertions
  (`_get_assertion_data`) and the curated field mappings
  (`curated_values_from_snapshot`), in the real refresh's order, over a snapshot
  of the person's provisional rows only (`OmopSnapshot` rebuilt with
  `dataclasses.replace`, with its own per-refresh caches). Nothing else runs;
  computed fields (BMI, scores) are not recomputed from a provisional input.
- **One reader list, not a copy (#2646):** `refresh_patient_record` and the pass
  both read `_section_readers()`, the pass runs the subset
  `provisional_section_readers()` in the real order, and the curated-mapping step
  is the shared `_curated_values`. A new or reordered reader in the real refresh
  therefore reaches the pass; a differential test holds every field the pass sets
  to the real refresh's value.
- **What is kept:** a value only for a field the real record leaves empty
  (`_is_empty`), that is not a pending user edit (the set before the refresh and
  the one after, so an explicit clear stays clear), and that has a confidence
  column. A companion the reader sets with it (`hemoglobin_level` and its
  `_units` beside `hemoglobin_g_dl`) is kept at the same confidence.
- **Confidence per field** is the lowest among the source-code groups that
  decide it: the pass re-reads the provisional rows once per (table, source
  value) group with that group removed, and a field whose value changes is that
  group's. A field no single group decides (several codes agree) takes the
  lowest confidence of all. Past `MAX_ATTRIBUTION_GROUPS` (40) groups every kept
  field takes the lowest of all, which never overstates and bounds the cost.
- **PATCH mirror** (`mirror_user_edit`, at the end of `_patch_record`): a
  proposal stays only while the record still has no answer for the field. An
  edited field, and any alias the edit filled in (`hemoglobin_level` after
  `hemoglobin_g_dl`), takes the record's value and loses its confidence; every
  other approved field follows the record too, so a computed field moves with an
  edit. A person with no provisional row has nothing to mirror. A failure is
  logged; the edit is already saved.
- **Clears and misfits:** the real clear markers are laid among the provisional
  rows before they are read, so a proposal older than a user's clear cannot
  refill it (matched on concept and source value, as derivation does); a
  proposed number or text that does not fit its column is dropped alone, as the
  real refresh drops it, and does not cost the person's other proposals.
- **Read: an overlay on the normal record read.** There is no separate
  endpoint and no role gate. `patient-records/{id}/` and `patient-records/me/`
  run the serializer over the record with a proposal laid over each field that
  has a confidence mark and that the live record still leaves empty
  (`provisional_record.overlay`), so a stale row can never override approved
  data, and add `provisional_fields` to `patient_info`: field to confidence
  percentage, `{}` when nothing is proposed. Access is exactly the real
  record's: a patient reads their own, a verified representative and a doctor
  the patient's, with the same demo-org de-identification; an analyst reads no
  patient, so no proposal either. Nothing is written: the overlay is a copy.
  The PHR APIs and the patient list are overlaid too (#2660, below).
- **Switch off means nothing:** with `PROVISIONAL_DATA_ENABLED` off no pair is
  written or mirrored, no provisional measurement is unit-read, the overlay is
  the record itself and `provisional_fields` is `{}`. A pair stored while the
  switch was on is simply never served; `backfill_patient_records --all` after
  turning it on refreshes one that has gone stale.
- **Not done:** the frontend (badge, show-provisional toggle, the untouched-form
  PATCH test), and the per-organization/domain floor. HealthTree's
  `patient_record` row count is still unread, so full-versus-sparse stays open.

### As built in #2660: the PHR APIs and the patient list

The same proposals, to the same audiences, on the other screens.

- **PHR owner reads** (`/api/v1/phr/`). A section opts in with
  `show_provisional = True` (About me, Diagnoses, Status, Labs, Lab history,
  Procedures); the default is off, so share options, consent and entries see the
  approved record. For an opted-in section the record read in `PhrView.get` is the overlay copy (`provisional_record.overlay`),
  so About me and the primary cancer carry `confidence` on a value a proposal
  filled (the lowest confidence among the fields an entry is built from). Labs
  (list, history), Diagnoses and Procedures also list **rows** from proposed
  mappings, each with `confidence`, and a section with only proposals is
  `ready` in status. Real beats provisional: on one date a real lab result is
  ordered before a proposed one, and a proposed condition or procedure is
  dropped when a real row already holds its concept
  (`provisional.without_shadowed`).
- **Switch off means nothing.** Every PHR clinical-row query goes through
  `provisional.real_or_provisional`, which filters `confidence IS NULL` unless
  the caller asked for proposals **and** the switch is on. Before this change
  the PHR sections read every row, so a proposal would have shown as an
  unmarked fact; now the default of every reader is approved-only.
- **Approved-only by design** (`show_provisional = False` on
  `SharedRecordMixin`, and no opt-in elsewhere): share recipients and the share
  catalog, the record export, Medications (a patient answers "did you take
  it?" against a row, which a proposal must not stand in for), Therapy lines,
  What's new (it counts what arrived from care centers), and the blood type.
- **Left out and why:** Genetics and Imaging read composite services in
  `omop_core` (genomics variants, imaging studies) that combine many rows, the
  composite-reader work in #2534; they are untouched here.
- **Patient list** (`PatientListSerializer.to_representation`): with the switch
  on, the row's empty columns (stage, ECOG, the status and biomarker summaries)
  take the proposal and the row carries `provisional_fields`. The page's
  queryset `select_related`s the pair, so the query count is flat in page size
  (`test_the_list_costs_the_same_queries_for_one_patient_or_many`). Access is
  the list's own: a reader who cannot list a patient, an analyst included,
  gets no proposal for them.
- **Rules the review added.** A real result is a test's `latest` whatever the
  proposals' dates (a proposal is the headline only when no real result
  exists); proposals are listed beside it in `recent` and `history` with
  `confidence`. A real condition names and dates a cancer entry; proposals in
  that family are listed apart under `proposed` (name, date, confidence) and a
  family only a proposal answers is headlined by it with its `confidence`.
  A proposed procedure is hidden only by a real one with the same concept **and
  the same date** (a repeat biopsy shows); a proposed condition is hidden by a
  real one of the same concept. `provisional_record.NEVER_OVERLAID` is an
  explicit denylist (identity, contact, and every field the record serializer
  redacts or withholds; a test holds it to those sets) that the overlay never
  touches, whether or not a mark exists for the field.
- **List filters and sort are approved-only.** The patient list shows an
  overlaid stage or disease status, but its filters (`stage`, `disease`, the
  status filter) and ordering read the stored columns, so `?stage=II` does not
  return a patient whose stage II is only a proposal. Making them follow the
  overlay would need a join to the provisional pair and a coalesce in every
  filter and sort, which is not flat or cheap at list scale; the simpler rule
  is chosen, pinned by a test, and the provisional picture is the row's display.
- Tests: `tests/test_provisional_phr.py`.

### As built: a mapping's confidence on the dialog (#2668)

A provisional row needs a proposed mapping with a confidence, and the proposals
come from Code to Concept and Concept to Code (the ETL proposes no concepts). The
column is `suggestion_confidence`; it stays on a mapping when it is approved. The
Edit Mapping dialog shows it as a bare percent to the right of the status
dropdown, with a **Calc** button that scores the destination chosen now through
`POST /api/v1/code-mappings/score/` (the rankers, with that concept guaranteed in
the pool), so a hand-picked destination can reach the floor and yield provisional
rows. A score is never invented (no ranker answer, or a ranker that preferred
another concept, means no number and a note), and it is never typed in: the server
signs what Calc or Suggest computed for a source and destination, and a save stores
a confidence only through that token, so no mapping writer can manufacture the
evidence provisional data trusts. The token binds the source, destination and a hash
of the description scored, covers only a score a ranker stood behind, and a stored
confidence is cleared when the destination or the source is re-pointed; a Concept to
Code preview's own score is trusted only while the preview is under a day old and
still describes the row. Details in the Code Mapping Suggest section of
`AGENTS.md`.

### As built in phase 1b (#2530)

Generate OMOP is the writer built so far; the lookup contract and FHIR sync
are not. What the code does, where the plan left a choice:

- **Which proposals qualify** (`provisional_proposal`): unscoped (no
  organization, because the per-patient inventory cannot say which hospital a
  code came from), ranked, at or above the floor, and a standard valid
  concept in the table's own domain. Proposals for one code under different
  source systems that disagree on the destination write nothing; agreeing
  ones take the lowest confidence. When the person's inventory
  (`PatientSourceCode`) says which systems the code arrived under, every one
  must have a qualifying proposal; with no inventory row the lookup is by
  code alone, as the approved-mapping lookups are. Promotion skips a person
  whose code also arrived under a system with no approval to the destination
  (`_persons_coded_elsewhere`). `suggestion_confidence` (0-1) becomes the
  percentage by rounding.
- **Writers.** `_create_missing_clinical_rows` creates a row for a code with
  no approved mapping and no row yet. `resolve_person_source_codes` turns a
  code's unresolved rows (concept 0 or an `HK-*` placeholder) into provisional
  rows, with the switch on.
- **Self-correcting resolve.** The same pass rewrites a provisional row whose
  proposal moved or changed confidence, and takes one back to unresolved when
  its proposal no longer qualifies (rejected, moved off the concept, below the
  floor, deleted, or the switch off), so a writer that moves a proposal
  without calling `revert_provisional_rows` cannot leave a row behind. That
  correction needs no switch.
- **Promotion** happens in `repoint_clinical_rows` (the old concept set
  carries the proposed concept, as `deferred_repoint_concept_ids` already
  does), in `promote_provisional_rows` for an approval at the concept the
  rows already sit on (called by the repoint job), and in resolve. Rows keep
  their confidence through the collapse and lose it afterwards, so a real row
  for the same event survives whatever the primary keys say (the collapse
  orders real rows first, #2584). Promotion and resolve delete only
  provisional duplicates, never a real row, so a keyed real row is safe.
- **Reverts** (`revert_provisional_rows`): a rejection or a moved proposal in
  `_upsert_source_code_mapping`, Suggest's Replace, and a CSV upload that
  points a proposal elsewhere. A patient-page save reverts that patient only.
  Nothing is deleted. Revert and promote match rows on the code only, never the
  import description, and revert runs in the table the rows are in before any
  relocation the same save queues. A writer that keys a provisional row on
  display text (the ETL, phase 2) will need that matching revisited.
- **Table relocation** keeps a row provisional: it moves with its concept and
  confidence, because the proposal it follows has not changed.
- **Readers that bypass `patient_record` leave provisional rows out** (#2530):
  the provenance lookups (`_lookup_measurement`, `_lookup_observation`,
  `_lookup_condition`, `_lookup_drug_exposure`, `_lookup_procedure`,
  `_lookup_sct`), so a guess is never the "selected" source of an approved
  value; the outbound FHIR export (`build_fhir_bundle`), always (see "Provisional
  data is never exported"); the patient list's freshness date
  (`annotate_context`); and the diagnosis context reader (`condition_contexts`),
  matching `medication_contexts`. `_lookup_genomics` reads the snapshot, which
  is real-only already.
- **Bulk resolve totals** count the new results: `SourceCodeResolveRun` has
  `provisional` and `reverted` columns (migration 0344), the run-status
  endpoint returns them, and the Patients dialog summary shows them.
- **`snapshot.provisional_measurements`** is read through
  `_read_values_in_source_units` like the real list, so the provisional pass
  (phase 1c) never has to prove a unit itself.
- **Not yet:** `code-mappings/lookup/` and the ETL (phase 2), and FHIR sync
  (#2581).

## Derivation

`refresh_patient_record` builds one snapshot of a person's rows and runs 26
section extractors over it. That pass is **unchanged**, with one rule added:
the snapshot's lists hold only rows whose `confidence` is null. Without that
filter a provisional row would become an approved value in `patient_record`,
which breaks invariant 3, so the filter and its test are the first change in
phase 1.

Provisional rows are split out of the same five table reads, in memory, into
`snapshot.provisional_*` lists. No query is added, so `BulkOmopWriteTest`'s
query-count assertions hold. A person with no provisional rows skips the
second pass entirely.

### The provisional pass

After the real record is derived, a second pass builds the person's row in
`patient_record_provisional`:

1. Start from the real `PatientRecord` values, all of them: demographics,
   user-entered fields, and everything derived. The provisional row is a full
   picture, so a consumer reads one table.
2. Run the **phase 1 readers** over a snapshot made of the provisional lists
   only, and keep a value only for a field that the real record leaves empty
   and that is not in `user_edited_fields`. The extractors are reused
   unchanged; the field-name allowlist, not a new code path, is what limits
   the pass.
3. Write the percentage of each kept value into `patient_record_confidence`
   under the same column name. A field whose value came from a real row has a
   null there.

Both new rows are written in the same transaction as `patient_record`, so
`derivation_version` and `derived_at` describe all three, and
`backfill_patient_records` rebuilds all three. A stale-marking
(`derivation_version=0`) therefore also covers the provisional pair.

### Phase 1 readers

| Reader | Field attribution |
|---|---|
| `_get_laboratory_data` and `_latest_loinc_measurement` | `resolve_lab_code_fields` names the code for each lab field |
| blood counts (`_latest_blood_count_measurements`) | `BLOOD_COUNT_FIELDS` |
| `curated_values_from_snapshot` | the approved `FieldConceptMapping` names the concept and source value per field |
| assertion fields (`_latest_assertion_rows`) | the assertion code per field |

### Readers that stay real-only in phase 1

Therapy lines, staging, disease status, cytogenetics, genomics, cancer
details, SCT and the other composite extractors combine many rows into one
answer and cannot attribute a confidence to a field without reworking each
one. The provisional pass does not run them: those columns in the provisional
table equal `patient_record`. A later phase can take them one at a time; the
rule for a composite field is the **minimum** confidence of the provisional
rows that joined its answer. JSON-typed fields were in this group; the lists
a reader can run over provisional rows alone are now covered
([List fields as built](#list-fields-as-built-2534)).

### Composite readers as built (#2534)

Two composite readers join the pass, over provisional conditions and drug
exposures only, with the phase 1 rule (a value only where the real record is
empty and nobody typed one; the lowest confidence among the rows that decide it):

| Reader | Fields | Deciding rows |
|---|---|---|
| disease (`_get_disease_data` over provisional conditions) | `disease`, `disease_slug`, `diagnosis_date`, `diagnosis_date_precision`, `condition_clinical_status` | the cancer `_primary_cancer_condition` picks; its confidence is every field's |
| medications (`recent_medications`, shared with the real reader) | `concomitant_medications` | the provisional exposures in the list: of the last ten, the five newest, a nameless one dropped (not replaced by an older row), so fewer than five are possible |

The disease reader runs only when a provisional condition is a cancer, because
without one its fallbacks take a diagnosis date from any condition and a status
from the newest one. The group is offered only when the record has no disease or
names the same one (same slug): a second cancer's dates must not fill the first
one's gaps. Left out: therapy lines (they read persisted Episodes, which a
proposal must not create), staging, cytogenetics, SCT and cancer details (they
read curator-written observation source keys that a proposed concept never
carries), the JSON lists (now covered: see
[List fields as built](#list-fields-as-built-2534)), and
`active_infection_status` (a "none found" over a provisional-only view says
nothing). A patient whose
approved record already holds these fields, such as one with many approved
rows, shows nothing new from them: real beats provisional.

Rules worth knowing, each held by a test:

- **The chosen cancer is the real ranking's, not the most confident.** The primary
  cancer is picked by `_primary_cancer_condition` (not resolved, has a current
  line, has a line, not in remission, newest). A 55% row that wins on recency
  gives every disease field 55, and a 97% row that loses gives none.
- **A typed disease blocks the whole group.** An edit of `disease` or
  `disease_slug` (including a cleared disease) keeps all five fields real; the
  PATCH mirror treats the group as edited together, so no field stays attributed
  to a proposal's cancer. The reader fails closed: a typed disease with no slug
  matches no proposal's slug, so it blocks the group too.
- **A curated value for the same field wins.** A field a phase 1 reader (a curated
  mapping) already proposed is not taken from a composite reader, as the real
  refresh lets the curated value win.
- **A reader costs only its own fields.** Each runs in its own savepoint, so an
  exception, a database error included, leaves the other reader's proposals and
  the pass's transaction alone.
- **Cost.** The disease reader reads nothing when the record already holds every
  field of the group, and a cancer's slug is worked out once per row.
  `_condition_slug` memoizes on the row object (one snapshot), which also saves
  the real refresh a category lookup per cancer per call.


### List fields as built (#2534)

Five kinds of JSON-typed field carry a confidence now: `disease_slugs`,
`active_malignancies`, `prior_procedures`, `genetic_mutations` and the
`genomics_*` markers (`PROVISIONAL_JSON_FIELDS`, 46 columns; migration `0360`
adds only their confidence twins, and its reverse drops them, which loses marks
and nothing else). The scalar rule "only where the record is empty" does not
fit a list the record may already fill, so a list has its own:

- **Real items are never replaced; provisional items are added.** An item is
  added only when no real item says the same thing (`_item_key`: a string by its
  casefolded, trimmed text; a procedure by its concept id, or its name when the
  concept is unmapped, and its date; a variant by gene, variant, feature, marker
  key and test date, ignoring origin, interpretation and the row id; text compares
  casefolded, so a typed `TP53` matches a proposed `tp53`). A
  user-edited list (`user_edited_fields`) is never added to, and a typed or
  cleared `disease` / `disease_slug` adds no `disease_slugs` proposal, as it
  blocks the scalar disease group.
- **The provisional column holds only the added items**, never a copy of the real
  ones. The reader lays the live items first and the stored ones after
  (`overlay`), so the column cannot go stale against the record: an item the
  record has since gained stops being provisional by itself, and reading an
  overlay again adds nothing. The picture of a list is made at read time.
- **One confidence per field: the lowest among the added items.** Per-item
  confidence is not stored. The record shows one indicator per field, the lowest
  is never an over-statement, and a sidecar would need a row-to-item attribution
  for each reader. A reader who must tell the items apart gets them from
  `provisional_items` (below); a sidecar can be added later without changing the
  rule.
- **Which items are proposals** travels with the record as `provisional_items`,
  `{field: [added items]}`, on the patient-record detail read only, next to
  `provisional_fields`; it is absent when nothing is added. The patient list and
  the PHR read the **approved** lists (`overlay(record, lists=False)`): a list's
  summaries (genomics, data gaps) and the PHR's Genetics readiness must not take
  a proposal for an approved finding, and the list payload carries no variant
  dicts. The frontend treats `provisional_items` as read-only (`LIFECYCLE`).
- **A PATCH never turns a proposal into approved data.** The editor saves the
  whole record it read, and a list it read holds the proposed items too.
  `apply_patient_record_patch` removes the items named in `provisional_items`
  from an incoming list before anything else: an echo of the merged list is a
  no-op (the real list and `user_edited_fields` unchanged, still a proposal), and
  a genuine edit stores only the real and the typed items. This covers every
  list field, `genetic_mutations` and the `genomics_*` markers included
  (`disease_slugs` is read-only in the record serializer, so no PATCH writes it).
  Consequences worth knowing:
  - **There is no PATCH path from a list proposal to approved data.** A typed item
    identical to a proposed one is dropped as an echo (a 200 that stores nothing).
    Approve a list proposal through its mapping or row approval, not a PATCH. The
    editor keeps the list's mark while it still holds proposed items
    (`withoutMarks` in `provisional.ts`), so the UI never shows it as approved
    while the server keeps it proposed.
  - **A genuine edit of a list drops every proposal for that list**, the untouched
    ones included: a user-edited list (`user_edited_fields`) is never added to
    again, and the PATCH mirror clears its mark.
  - **The PATCH response is the approved record** (no `provisional_fields` or
    `provisional_items`), as for the scalar fields: a client that needs the marks
    re-reads the record.

| Field | Reader (shared with the real refresh, over provisional rows only) | Confidence of an item |
|---|---|---|
| `disease_slugs` | `_get_disease_data`: one slug per provisional cancer | the lowest among the cancers with that slug |
| `active_malignancies` | `malignancy_rows`, the real current-condition rule | the lowest among the current conditions giving that name |
| `prior_procedures` | `procedure_item`, the real item | the lowest among procedures giving that item |
| `genetic_mutations`, `genomics_*` | `_get_genetic_mutations` over provisional measurements and observations | the finding's own row, lowered by any linked component row |

Left out, with the reason: the therapy-line id lists and `medication_contexts`
(persisted Episodes and contexts, which a proposal must not create), the SCT
lists (curator-written observation keys), and a `genomics_*` marker the reader
*derives* from findings (a derivation over only the proposed parents says
nothing about the others; today's derivations are stubs, and a test holds the
guard). The calculated fields that read a list (`no_other_active_malignancies`)
still equal the real record. A reader costs only its own fields (its own
savepoint), and the lists add no query per item.

The Genomics tab lists variants from its own endpoint over approved findings, so
a proposed variant appears in the patient-record detail read but not yet in that
table, the patient list or the PHR; the other lists have no control on the
provider page.

Cost: a person with no provisional row reads nothing (no query). The disease
read runs once per refresh for the scalar group and the slug list together, and
the cancer names of the provisional conditions are canonicalized with one
`DiseaseCategoryMapping` query (`canonical_disease_names`) instead of one per
row. The migration adds columns to `patient_record_confidence`, so it waits
behind a running derivation that holds a lock on the table.

### Computed fields

`recompute_patient_record_fields` computes BMI, scores and aliases from stored
fields. On the provisional row it runs again over the overlaid values. A
computed field whose inputs include a provisional value is itself provisional
at the minimum of its inputs' confidences.

**As built (#2661).** `provisional_record.COMPUTED_INPUTS` declares, for each
calculated field that has a confidence column, the stored fields it is
calculated from: `bmi` (weight, height and their units), `tnbc_status`,
`hr_status`, `metastatic_status`, `renal_adequacy_status`,
`no_active_infection_status` and `b_symptoms`. `computed_proposals` lays the
pass's proposals over a copy of the real record, runs the real calculation
(`_compute_derived_fields` and `combine_b_symptoms`, shared with the real
recompute) and keeps a result only when the record has no answer for the field,
nobody typed one (`user_edited_fields`), no reader already proposed it, no
active `FieldFormula` owns it, and a proposal is among its inputs. Its
confidence is the lowest among the inputs that are proposals; an approved input
carries no mark. Nothing is saved from the copy, so `patient_record` never
holds a provisional value; approving the inputs lets the real refresh calculate
the field and the mark goes. The PATCH mirror recalculates a field still on a
proposal after an edit to one of its inputs, and drops it when the edit leaves
it nothing to calculate from. The vitals reader joins the provisional readers
so a proposed weight and height exist; a proposal is read only when its unit
is already the one the reader assumes (kilograms, centimetres or metres,
mmHg, per minute), because a proposal is not unit-converted on write. A vital
whose newest proposal is in another unit is dropped whole, so an older reading
never stands in as the current one, and a proposed weight or height is dropped
when the record's own unit for it differs, so a number is never shown with the
wrong unit. A failing recalculation in the PATCH mirror costs only the
calculated fields, not the edit's own mirror. Left for later: fields calculated from
a JSON input (`no_other_active_malignancies`, FLIPI, GELF), the free light chain
ratio and `measurable_disease_imwg` (they read one lab event's pair), age,
`last_treatment`, and fields owned by a formula.
Custom fields (`custom_fields` JSON) are phase 3.

Decision (2026-11-17): with the switch on, computed fields use provisional
inputs, so a patient with a proposed weight and height sees a BMI. The
computation runs over the overlaid values without saving, and the result is a
provisional field at the lowest confidence of its inputs
([#2661](https://github.com/healthkey-ai/promop/issues/2661)).

### User edits

A PATCH to `patient_record` writes the value as today. Because direct saves do
not call `refresh_patient_record()`, the same save also writes the edited
fields into the provisional row and nulls their confidence, so the provisional
picture never shows a proposal where a person has just typed a fact. The
edit is also protected on the next derivation by `user_edited_fields`, as the
real value is. The confidence table is read-only to every client.

A page that shows the provisional picture must **not** use it as the PATCH
baseline. Native autosave sends its whole GET representation, and a
provisional value echoed back would be saved as an approved one. The page
keeps the real record as the baseline, diffs edits against it, and sends only
changed fields; a frontend test sends an untouched form and asserts an empty
PATCH.

### Performance

The provisional pass costs one extra row upsert per table and no extra
snapshot query. Staging holds about 3,000 patient records in 21 MB, so a full
provisional row per person is small there. The HealthTree instance has its
own database and was not measured; the first task in phase 0 is to count its
`patient_record` rows. If the count is large enough that two more wide tables
matter, the provisional table can be made sparse (a row only for a person
with at least one provisional value, the read API falling back to the real
record) at the cost of a coalesce for anyone reading it in SQL.

## Schema

**Clinical tables.** `confidence = SmallIntegerField(null=True)` on the five
models (migration `0339`), with a check constraint `0 <= confidence <= 100`, and
a partial index per table on `(person_id) WHERE confidence IS NOT NULL`. The
column is nullable with no default, so the `ADD COLUMN` is a catalogue change on
every table including `measurement`. It is a signed `smallint` on purpose:
`PositiveSmallIntegerField` adds a `>= 0` check to the `ADD COLUMN` statement,
and a check on an added column scans the table under an exclusive lock. The
range check is added `NOT VALID` and validated in a second statement, which
blocks no writes. The partial indexes are built with
`AddIndexConcurrently` in a non-atomic migration: a plain build scans the
whole table under a lock, and the Cloud Run migrate job has an hour (#2021).

**`patient_record_provisional`.** One row per person, primary key
`person` (one-to-one). Every concrete `PatientRecord` column except the
identity key and lifecycle columns is cloned with the same name and type, at
class-creation time from `PatientRecord._meta` (`field.clone()` in a loop
beside the class), not hand-typed. Constraints, indexes and the `organization`
foreign key are carried only where a reader needs them (tenancy filtering uses
`organization`). `makemigrations` sees the clones as ordinary fields, so a
field added to `PatientRecord` gets its provisional twin in the same
migration, three `AddField` operations instead of one.

**`patient_record_confidence`.** One row per person, primary key `person`.
One nullable `PositiveSmallIntegerField` per non-JSON field in
`_OMOP_DERIVED_FIELDS`, 274 at first, named exactly as the field is, plus one
per covered JSON list (`PROVISIONAL_JSON_FIELDS`, #2534). Generated the same
way.

A parity test fails the build when the three tables drift: the provisional
table must have every cloned column of `patient_record`, and the confidence
table must have exactly the derived non-JSON fields and the covered JSON lists. The model pair is
classified in `omop_core/services/instance_data.py` like `PatientRecord`:
derived and re-derived after `copy_patient`, not copied
([Copying data between instances](../AGENTS.md#copying-data-between-instances)).
`tests/test_instance_data.py` fails until they are.

Creating the two tables needs no lock on existing data, and no backfill
migration: `backfill_patient_records --all` populates them, and until a person
is derived the API serves the real record for the provisional view.

**Bulk write identity.** `confidence` stays outside `_UPSERT_KEYS`, like the
concept column: an ETL re-run after an approval converges on the promoted
row instead of inserting a second one. Only the concept column is rewritten
on a matched row today; `confidence` joins it, and only toward certainty: a
match may set a row's confidence to null (the caller now has an approved
concept) but never put a number on a real row.

## API

The provisional picture is not a separate resource. The existing read
(`/api/v1/patient-records/{person_id}/` and `/me/`, v1 only) lays each proposal
over a field the record leaves empty and adds the marks, so every audience that
may read a patient reads what is proposed for them, with no toggle:

```
GET /api/v1/patient-records/{person_id}/
  200 {"patient_info": {<every field, a proposal where only a proposal answers>,
                        "provisional_fields": {"hemoglobin_g_dl": 82, "her2_status": 91}},
       "user": ...}
```

`provisional_fields` lists only the fields shown from a proposal, so a fully
approved person returns an empty object, and so does every read with the switch
off. A person not yet derived reads as the real record. A client must not send
the overlaid values back as edits: diff against the real record, or send only
what the user changed. Foundations that need it in bulk read the two tables by
column name, joined on `person`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PROVISIONAL_DATA_ENABLED` | `False` | master switch; off, every writer behaves as today (a clinical write carrying a confidence number is refused with 400; null is always accepted) and the provisional pair is neither written nor served, and a record read carries no overlay |
| `PROVISIONAL_CONFIDENCE_FLOOR` | `70` | least confidence a provisional row is written at |
| `Organization.accepts_provisional_data` | `False` | FHIR sync for this organization passes `accept_proposed` |

The floor and the auto-approval threshold of #1532 are the same number in
spirit: a proposal at or above the auto-approval threshold would become real,
so the floor only matters for the band between the two. Build the floor first;
#1532 can read it.

## What consumers see

**Existing consumers of `patient_record`.** Nothing changes: the legacy
`patient-info` path, the federation host (`ht-phr`), PRism, FHIR export and
every PATCH read and write approved data only. This is the main reason for the
split.

**Patient page, every role that reads the patient.** Where the switch is on, a
patient (on their own page), a verified representative and every professional who
may read the patient see proposed values **by default**, with no toggle. The
record read lays each proposal over a field the record leaves empty and returns
`provisional_fields` (field to confidence). The page shows an amber "Provisional
82%" indicator after the label, with a title saying the value came from a
mapping not yet approved (a patient sees "not yet confirmed"), and a clinician's
link to the mapping dialog for its code. An edit on a provisional field saves
to the real endpoint as an approved value, and the indicator goes. The page
keeps the real record as its PATCH baseline. The purpose is to give patients a
view of their data while the concepts are mapped to codes. Analysts read no
patient, so they see no proposals. With the switch off nothing is computed,
stored or shown to anyone ([#2659](https://github.com/healthkey-ai/promop/issues/2659)).

**Confirming a record that shows proposals (#2761).** A patient's "Confirm my record"
(`POST /api/patient-info/me/confirm/`) says what the page shows is accurate, so
it is not offered, and the server refuses it, while a value the patient can see and
edit is still marked Provisional:

- **What blocks.** The scalar fields the overlay marks for the patient's own record
  (`attestation_blockers` in `provisional_record.py`: `overlay(record, lists=False)`
  marks) that a patient tab renders, and the patient can edit. "A patient tab renders
  it" is `patientVisibleFields.json`, beside the frontend code: the confidence columns
  named in the General, Labs, Behavior and Wearable tab components, generated by
  `python -m omop_core.services.patient_visible_fields --write`. The one file is
  imported by the frontend (whose image is built from `frontend/` alone) and read by the
  server (every backend image carries `frontend/`). A backend test and a frontend test
  fail when the tabs and the file drift. Of those, a field with no
  write path of its own (calculated, mirrored, a unit picker) does not block, since it
  clears with the field it follows, and a shown field the write descriptor does not
  know does. Left out: the fields the General tab renders only outside patient mode (`validated`,
  `validated_by`, `validation_date`: staff see them, and a patient's PATCH refuses them,
  so a mark on one could never be cleared by a patient), a field no tab renders (`concomitant_medications`, the
  diagnosis date, `blood_type`, ...), which the sweep proposes but is marked only in
  the record read; the list fields, which no patient tab shows; the **disease tab**
  (its fields show only for the patient's own disease, which the page decides from the
  disease text, so a static list cannot say one is on screen); the treatment tab
  (therapy lines have their own dialog); and the controls that bypass `Field` (GELF,
  FLIPI, treating institution), which show no mark. **A proposal on one of those is
  shown, or part of what the patient is attesting to, without blocking:** this is a
  known gap, open with the indicator gap above. With the switch off, or no proposal on
  display, nothing blocks.
- **The endpoint.** The record is locked first (as derivation does), so a sweep cannot
  add a proposal between the check and the save. 409 with
  `{detail, provisional_fields: [names], count}` and nothing stored on the record or
  the person. The page applies the same rule to the marks it holds
  (`attestationBlockers` in `PatientInfo/provisional.ts`, over the same JSON, then the
  write descriptor once it has loaded), so the banner and the endpoint agree. Before
  confirming, the page sends any edit the autosave has not sent yet; a 409 it did not
  predict makes it read the record again, and a save of a marked field does too, so
  marks the server cleared (the disease group, a calculated field) update the banner.
- **The page** (General tab, patient mode only). Not confirmed, with blockers: an amber
  banner, "N values are marked Provisional (not yet confirmed)", saying to check each
  one and enter the right value in its field (entering the same value keeps it), and
  that Confirm Record returns once none are marked, with no button. Confirmed, with
  blockers: "Record confirmed on <date>" and "N values are marked Provisional and not
  covered by that confirmation", Re-confirm hidden. Otherwise unchanged.
- **How a block is lifted.** By entering your own answer in the marked field: that saves
  it as approved data and drops its mark (the PATCH path above), and the disease group goes
  together. For a number or text field that means retyping a value; for a select or a
  boolean, picking another option and then the shown one again (choosing the option that is
  already selected sends no change), so the banner says so. **Explicitly rejecting a
  proposal is still open (#2671), so a patient who disagrees must enter their own value:**
  a PATCH of `null` on a number (or other non-text) field the approved record already
  leaves empty is a no-op and the mark stays. A text or select field is different: `''` is
  accepted as an edit, which clears the mark and stores `''` in the approved record, an
  answer of "nothing" and not a rejection. A patient who does not know the right value
  therefore stays blocked on a number field until a clinician or the mapping settles it.
  Tests pin both behaviours. A real "reject" needs a rule for how a rejection persists
  without freezing the field against later approved data (`user_edited_fields` would), so
  it is its own change.
- **What is stored.** `validated`, `validated_by` and `validation_date` still say only
  that the patient pressed the button; the endpoint guarantees that no value from the
  list above was marked Provisional when they did. A proposal that arrives later (a
  sweep) does not undo an earlier confirmation, and the page says so; it blocks the next
  one. Confirmations made before this change are not revisited.

**PHR APIs and lists.** The same audiences see the same proposals on the PHR
labs and other APIs and on the patient list, with `confidence` on each, never a
different answer on another screen ([#2660](https://github.com/healthkey-ai/promop/issues/2660)).

**Patient OMOP tab and Source Codes tab.** A provisional row shows the badge
in its row. The "Generate OMOP" banner says how many provisional rows the run
wrote, and the per-code detail reports `outcome: 'provisional'` with the
confidence.

**Lab results API.** A provisional row's entry carries `confidence`, and the
reads that choose "the latest result" prefer a real row over a provisional
one on the same date, as derivation does.

**A foundation that wants provisional data.** It reads
`patient_record_provisional` for values and `patient_record_confidence` for
the trust level, joined on `person`, with identical column names in both, or
the API above. It reads `patient_record` for approved data only.

**Readers of the clinical tables.** `patient_record` is safe, but anything that
reads `measurement`, `observation`, `condition_occurrence`, `drug_exposure` or
`procedure_occurrence` directly would count a provisional row as a fact. The
existing `is_erroneous=False` filters (61 call sites) are the inventory to
review. Each either keeps provisional rows deliberately (the patient page, the
OMOP tab) or adds `confidence IS NULL` (anything aggregate or outbound, and
FHIR export, which always excludes them). The review is a checklist
in the PR for phase 1, not a change at every site.

**Mapping pages.** The Code to Concept list shows, per proposed row, how many
provisional rows it currently backs, so a curator knows what an approval
promotes and a rejection reverts. #2263 (keep the confidence visible on the
existing-mapping view) is the same number and lands with this.

## Phases

### Phase 0: schema and contract (no behaviour change)

- Count `patient_record` rows on the HealthTree instance and settle full versus
  sparse provisional rows.
- `confidence` on the five clinical models, constraint and partial indexes.
- `patient_record_provisional` and `patient_record_confidence`, generated from
  `PatientRecord` and `_OMOP_DERIVED_FIELDS`, with the parity test and the
  `instance_data` classification.
- Serializers: `confidence` accepted on bulk and single row writes from machine
  credentials and staff, refused at concept 0 and from session users; outside
  the upsert key.
- `PROVISIONAL_DATA_ENABLED` and `PROVISIONAL_CONFIDENCE_FLOOR` read in
  settings.
- Tests: model round trip, constraint, serializer refusal, bulk write
  acceptance and refusal, upsert convergence on a promoted row, table parity.

### Phase 1: Generate OMOP, derivation and the page

- Snapshot filter: `patient_record` derives from null-confidence rows only.
  Written and tested first.
- `_create_missing_clinical_rows` and `resolve_person_source_codes` write and
  promote/revert provisional rows behind the switch.
- `repoint_clinical_rows` promotes on approval; `revert_provisional_rows` on
  rejection and on a moved proposal, wired into `_upsert_source_code_mapping`
  and Suggest's re-point.
- The provisional pass, the two table writes, the PATCH mirror, and the
  overlay on the patient-record read.
- Frontend: badge on `ClinicalField`/`Field`, real record as PATCH baseline,
  show-provisional toggle, PHR labs skip, Source Codes tab outcome.
- Tests: `patient_record` unchanged by a provisional row; derivation
  precedence (real beats provisional; provisional fills an empty field; user
  edit wins; revert clears); approval promotion; rejection revert; patient role
  refused; an untouched form sends an empty PATCH; React badge render and edit.

### Phase 2: the ETL path

Superseded for HealthTree by the server-side sweep, trigger and after-ingest job
(#2662, above): the ETL does not propose concepts and sends no `confidence`. The
lookup contract below remains for a caller that does want to write rows itself.

- `code-mappings/lookup/` `include_provisional` and the response fields.
- ETL repository change to write `confidence` and to re-send after approval.
- Lab results API handling. (FHIR export is settled: provisional data is never exported.)
- Mapping-page counts and #2263.

### Phase 3: wider reach

- Composite extractors (therapy lines, staging, genomics) one at a time, each
  with the minimum rule.
- JSON derived fields (done for the lists a reader can run over provisional rows, #2534) and custom fields in the confidence table.
- FHIR sync opt-in per organization. The PHR is the patient's source of truth, so
  this is a data-quality decision about what sync writes into the PHR, not the
  institution's consent.
- Patient-facing display policy.
- #1532 auto-approval reading the same floor.

### Provisional data is never exported

The PHR is the patient's source of truth, and provisional values are unconfirmed
proposals, so nothing that leaves the PHR carries them: the FHIR export
(`build_fhir_bundle`, which backs the patient-records export, the signed PHR record
export and the CLI command) filters `confidence IS NULL` and has no option to include
provisional rows, and any future write-back or upload to an institution must do the
same. Nobody is blocked from sharing: a patient who approves a value, by entering it
themselves or through an approved mapping, makes it real, and it then exports like any
other row. Saving a provisional value *in place* in the PHR is not an export and is
unaffected.

### Gaps before HealthTree can run with provisional data on (2026-11-17)

Status as of 2026-11-17, all behind `PROVISIONAL_DATA_ENABLED`. Built and on
`dev`: the confidence column, the Generate OMOP writes, the two tables, the
provisional pass, the PATCH mirror, the overlay on the patient-record read, the
page indicator, the PHR and list overlay, computed fields, the mapping
confidence on the Edit Mapping dialog (signed Calc score) and the server-side
sweep. To deploy to HealthTree GCP with it on, and have patients see proposals
without a click:

| Gap | Issue | State |
|---|---|---|
| Frontend: indicator on every provisional field, real record as PATCH baseline, no toggle (replaces the toggle in #2532) | [#2659](https://github.com/healthkey-ai/promop/issues/2659) | done (#2665); controls that bypass `Field` (GELF, FLIPI, treating institution, planned therapies) show no indicator yet |
| The PHR APIs and the patient list show the same proposals, with confidence | [#2660](https://github.com/healthkey-ai/promop/issues/2660) | done (#2667); Genetics and Imaging wait on #2534; list filters and sort stay approved-only |
| Computed fields use provisional inputs at the lowest input confidence | [#2661](https://github.com/healthkey-ai/promop/issues/2661) | done (#2666, #2672); JSON-input fields, FLC pair, age left |
| Proposals are stored with a confidence on the mapping, kept on approval, recomputed by Calc | [#2668](https://github.com/healthkey-ai/promop/issues/2668) | done (#2670) |
| Server-side provisional rows from proposed mappings (the ETL does not propose) | [#2662](https://github.com/healthkey-ai/promop/issues/2662) | done (#2679): unscoped proposals only; run `sweep_provisional_rows` after enabling; confirm HealthTree's ETL does not pass `organization_id` on Epic/Cerner lookups |
| A user cannot clear a displayed proposal (PATCH null on an already-empty field is a no-op) | [#2671](https://github.com/healthkey-ai/promop/issues/2671) | open |
| The patient's "Confirm my record" attestation is still offered while proposals are shown | [#2761](https://github.com/healthkey-ai/promop/issues/2761) | done: refused (409) and not offered while a marked field a patient tab shows and the patient can edit is on display; gaps: disease tab and bypassing controls do not block, and a proposal cannot be rejected (#2671); see "Confirming a record that shows proposals" |
| Measure, then enable on Render staging, then HealthTree GCP with `--update-env-vars`, `sweep_provisional_rows`, backfill, check patient 52971; rollback | [#2663](https://github.com/healthkey-ai/promop/issues/2663) | open: needs a go-ahead and HealthTree access |
| Composite readers (therapy lines, staging, genomics) and conditions/drugs/procedures feeding `patient_record` fields | [#2534](https://github.com/healthkey-ai/promop/issues/2534) | partly done: disease fields, `concomitant_medications` and the JSON lists (`disease_slugs`, `active_malignancies`, `prior_procedures`, `genetic_mutations`, `genomics_*`); therapy lines, staging, cytogenetics and SCT open |

The rule for all of them: with the switch off, no provisional data is computed,
stored or shown to anyone.

Each phase is its own PR against `dev`, with a migration in phase 0 only;
rebase on `dev` immediately before `makemigrations` and again before merge
(see [Migration conflicts](../AGENTS.md#migration-conflicts)).

## Alternatives considered

- **Companion columns on `patient_record`** (a `<field>_` beside each field).
  Rejected: it widens the flat table by 274 columns, and the provisional value
  would sit in the approved column, so any reader that ignored the companion
  would show a proposal as a fact.
- **One JSON confidence map on `patient_record`.** Rejected for the same
  second reason, and it loses the same-column-name property in SQL.
- **A database view over the real table.** Rejected: the frozen `patient_info`
  view (#448) shows how a view that mirrors the record drifts the first time a
  column is added. Generated tables with a parity test fail loudly instead.

## Open decisions

1. **Full versus sparse provisional rows.** Full means one row per person and
   a one-table read; sparse saves storage on a large instance. Settled by the
   row count in phase 0.
2. **Revert versus delete on rejection.** The plan reverts a rejected
   proposal's rows to concept 0 rather than deleting them, because the ETL
   would re-send a deleted row on its next run and because a row at concept 0
   is what ingest writes today. A row Generate OMOP created from the
   inventory, which would not otherwise exist, is the one case where a delete
   would be cleaner; it is kept as an unresolved row so that one rule holds.
3. **What a patient sees.** Settled 2026-11-17: where the switch is on, a
   patient sees their own proposed values, marked, by default. This reverses
   the phase 1 refusal.
4. **The floor's scope.** One deployment-wide floor in phase 1. A per-organization
   floor, or per domain (a provisional lab value is a smaller claim than a
   provisional condition), is a phase 3 setting if curators ask for it.
5. **Default view.** Settled 2026-11-17: provisional by default for every role
   that reads the patient, with no toggle; the site switch is the control.

## Related

- #1532 auto-approval threshold, #2263 confidence on the existing-mapping
  view, #2123 lookup sightings (the existing `provisional` keyword).
- [Code mapping lookup](code-mapping-api.md), [field concept mapping
  architecture](field_concept_mapping_architecture.md),
  [PatientRecord-first writes](patient-record-first-writes.md).
