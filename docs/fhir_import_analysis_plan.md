# FHIR import analysis plan

## Document ownership

This document is the forward-looking delivery plan for import-batch reporting
and FHIR import quality analysis. It records scope, intended contracts,
sequencing, dependencies, and acceptance gates. It must not become the source
of truth for behavior that has shipped.

As work lands, describe the verified implementation in
`fhir_import_analysis_architecture.md` and replace the corresponding detail here
with a short delivery-status entry and a link to that architecture section.
Runtime field/value inventories, deployment receipts, and batch exports belong
in generated or operational artifacts, not in either narrative document.

## Outcome

PRomop should show, on an organization's **Stats** tab, an **Import Batches**
section that answers four questions:

1. Did this ETL batch process all of the patients it intended to process?
2. For each patient, what was created, updated, left unchanged, skipped, or
   rejected, and at which stage did a failure occur?
3. Which source codes prevented standard-concept resolution, ordered by their
   actual impact, with a direct path into Code Mapping work?
4. Did the import make the patient records more complete, and what parser,
   mapping, write, or derivation work would improve the next batch?

The first release should support `~/etl`, but the PRomop contract must be
importer-neutral. FHIR upload, HealthEx, CureHub, and future importers should use
the same model rather than acquiring separate report tables.

## Current seams to extend

The delivery should build on these existing seams:

- `~/etl/airflow/dags/services/import_flow/` already distinguishes imported,
  skipped, missing, and failed items in `ImportRunReport`, and supports
  flow-specific counters.
- `FhirIngestionResult` and `FhirPatientIngestionResult` already retain
  per-patient row IDs, skip reasons, write errors, and created/updated totals.
- The ETL writer already groups and writes OMOP rows per patient, making the
  patient boundary the natural unit for reporting and retry.
- PRomop's v1 person and clinical-event endpoints are the authoritative write
  path. New integration behavior belongs under `/api/v1/`; the legacy API must
  not be extended.
- `SourceCodeConceptMapping` (SCCM) and
  `POST /api/v1/code-mappings/lookup/` are the governed source-code resolver.
  SCCM already has review state, encounter count, first/last seen timestamps,
  and the mapping ID needed for curation.
- `OrgDetail.tsx` already has an organization Stats tab. Import reporting
  belongs below its disease summary rather than on a second, unrelated stats
  page.

One gap must be corrected as part of this work: the ETL FHIR writer currently
uses the older direct concept lookup. Batch-aware ingestion must resolve source
codes through SCCM so that an approved curator decision wins and unresolved
codes enter the review queue. A proposed destination remains a hint and must
never be written as the effective OMOP concept.

## Reporting vocabulary

Counts need stable definitions or two pages will show plausible but different
totals.

| Term | Definition |
|---|---|
| Selected | Distinct patient work items frozen into the ETL run before processing. |
| Attempted | Selected items for which an attempt started. |
| Good patient | A patient whose final attempt finished with no write error or unresolved fatal patient error. Benign, explicitly classified skips do not make the patient bad. |
| Partial patient | The patient resolved and at least one useful write succeeded, but one or more rows or required stages failed. A partial patient is not counted as good. |
| Error patient | The final attempt failed before any usable result, or was explicitly marked failed by the importer. |
| Skipped patient | Intentionally not imported for a named, non-retryable reason. |
| Missing patient | Selected at enumeration but absent when loaded from the source. |
| Code occurrence | One source-coded clinical fact or FHIR component encountered before retry de-duplication. |
| Affected patient | One distinct batch patient containing a source code, regardless of repeat occurrences. |
| Resolved at import | The resolver returned an effective approved or authoritative direct destination when that attempt ran. |
| Unresolved at import | No effective destination was available. Proposed, rejected, missing-vocabulary, and ambiguous states remain unresolved. |

`total patients` in the headline should mean `selected`, not the number of
`PatientRecord` rows eventually visible. The UI should show the reconciliation
equation:

```text
selected = good + partial + error + skipped + missing + not_started
```

The end-batch operation must fail validation if this equation does not balance.
It may still close the batch as `completed_with_errors`; it must not silently
invent the missing category.

## Planned data model

Use reporting models owned by PRomop. Keep them out of the core OMOP tables;
they describe execution and quality evidence, not clinical facts.

### `ImportBatch`

One row per logical ETL run, with a UUID primary key.

Planned fields:

- owning `Organization` (required and immutable);
- importer/flow name, source system, source environment, and import mode
  (`incremental`, `backfill`, `reconciliation`, or `manual`);
- caller-owned `external_run_id` and optional Airflow DAG/run identifiers;
- status: `open`, `completed`, `completed_with_errors`, `failed`, `abandoned`,
  or `cancelled`;
- `dry_run`, importer version, parser version, mapping-contract version, and
  completeness-profile version;
- start, last-heartbeat, and completion timestamps;
- selected count declared at start, server-computed outcome totals at close,
  duration, and non-PHI JSON metadata;
- optional input artifact digest and source watermark. Store a digest or safe
  artifact locator, never a FHIR payload or credential;
- created-by service identity and audit timestamps.

Enforce uniqueness on `(organization, importer, external_run_id)`. Repeating a
create request with the same idempotency key returns the existing batch; it
must not create a second report for an Airflow retry.

### `ImportBatchPatient`

One row per distinct patient work item in a batch. This is the durable
many-to-many history between batches and patients.

Planned fields:

- batch and a caller-supplied opaque `client_patient_key`;
- nullable `Person` reference, populated after identity resolution;
- final outcome and operation (`created`, `updated`, `unchanged`, or
  `unknown`);
- attempt count and final-attempt reference;
- per-resource input, parsed, created, updated, unchanged, skipped, rejected,
  and deleted counts;
- skip-reason and unhandled-FHIR-resource counts;
- error stage, stable error code/fingerprint, retryability, and a sanitized
  operator-safe message;
- completeness before/after/delta summaries;
- first-attempt and final-attempt timestamps.

The client key must be stable within the batch but need not expose an MRN,
FHIR Patient ID, email, or name. Prefer a source-side HMAC or random work-item
ID. PRomop must never log it at normal log level.

Do **not** add a single `batch` foreign key to `PatientRecord` or `Person`.
Patients occur in many incremental batches, sources, and retries; a single
foreign key would retain only the latest run and make history misleading.
Person create/update requests should accept optional batch context, and the
server should upsert `ImportBatchPatient` membership instead.

### `ImportBatchPatientAttempt`

Retain one row per attempt so a retry that succeeds does not erase evidence of
the original failure. Store attempt number, caller idempotency key, start/end,
outcome, stage timings, safe errors, and the row/funnel counters returned by
the importer. The normal batch page can show only the final outcome while a
detail panel exposes retry history.

Do not store raw exception bodies: upstream responses can contain PHI. Map
errors to stable codes and retain a redacted message plus a fingerprint for
grouping. Full diagnostic artifacts remain in the ETL system under its own
access and retention controls.

### `ImportBatchWriteEvent`

Record the server-observed effect of each batch-context write: attempt,
idempotency/event key, resource type, row ID, and operation (`created`,
`updated`, `unchanged`, `deleted`, or `rejected`). This is the auditable source
for write totals and retry de-duplication.

Do not try to derive every write count from `ProvenanceRecord`. The current
bulk upsert path creates provenance only for inserted rows, and provenance's
existing uniqueness rule is not an attempt ledger. Add the attempt reference
to provenance where a clinical mutation legitimately creates a provenance
record, but use `ImportBatchWriteEvent` for complete batch accounting. The
event must contain identifiers and safe validation codes, not clinical values.

### `ImportBatchCodeObservation`

Store the source-code demand observed in this batch, aggregated by:

```text
(batch, source_vocabulary_id, normalized_source_code, omop_table)
```

Planned fields include source description, mapping reference when present,
total occurrences, distinct affected patients, first/last encounter, resolution
state at import, target concept at import, and current resolution fields used
to refresh the report. Preserve the raw source code subject to SCCM's length
and normalization rules; do not treat a code description as identity.

Including `omop_table` in the observation identity is intentional. SCCM is
currently unique on `(source_vocabulary_id, source_code)`, while the same local
code can be observed in more than one clinical domain. The batch report should
surface such a cross-domain collision as a mapping-contract error. Before
changing SCCM's own identity, audit existing rows and decide whether mappings
are truly global or must become `(vocabulary, code, domain)` scoped; do not
quietly choose one domain based on whichever batch arrived first.

Counts posted by a retried attempt must be idempotent. A caller-supplied
observation event ID, scoped to the patient attempt, should be recorded once;
retrying it returns the original result without incrementing either the batch
count or SCCM `Seen` again.

### Completeness evidence

Start with versioned JSON snapshots on `ImportBatchPatient`; normalize only
dimensions that later need cross-batch SQL analysis. A snapshot should carry:

- profile and version;
- applicable dimensions and why they apply;
- present/missing state before and after the attempt;
- mapped/unmapped status for applicable coded facts;
- source freshness and the newest clinical date by resource type;
- projection status: written to OMOP, reflected in `PatientRecord`, or stale;
- evidence counts, never raw clinical values.

Completeness must be profile-aware. A universal percentage would penalize a
source for data it never claims to provide and would make a breast-cancer
record look incomplete for lacking myeloma fields. Profiles should be selected
by import flow, FHIR capability/manifest, and disease context. Show dimensional
coverage and a numerator/denominator alongside any percentage.

PRomop should own the versioned completeness evaluator and profiles. The ETL
reports source capability and funnel evidence; after identity resolution,
PRomop computes the before snapshot from its data and computes the after
snapshot only after the final derivation/readback. This avoids duplicating
clinical completeness rules in two repositories.

## API contract

All new endpoints belong under `/api/v1/`. Use DRF serializers and the existing
ETL write permission patterns.

### Batch lifecycle

```http
POST /api/v1/import-batches/
Idempotency-Key: <stable ETL run key>

{
  "organization": "org-slug",
  "external_run_id": "airflow-run-id",
  "importer": "healthkey-etl",
  "flow": "fhir_extract",
  "mode": "incremental",
  "selected_patients": 120,
  "versions": {
    "importer": "...",
    "parser": "...",
    "completeness_profile": "..."
  }
}
```

The response returns `batch_id`, status, and server timestamps. Organization
ownership must be authorized from the authenticated service application; an
arbitrary header must not let a caller claim another org. If the transitional
global ETL service token is used, explicitly allow-list its ability to name an
org and audit that use. The target state is an organization-bound service
application or an equally explicit multi-org grant.

```http
POST /api/v1/import-batches/{batch_id}/heartbeat/
POST /api/v1/import-batches/{batch_id}/complete/
POST /api/v1/import-batches/{batch_id}/fail/
```

Completion is idempotent and server-computes totals from child records. A
client may submit expected totals for reconciliation, but cannot overwrite the
computed result. `fail` records a safe run-level error without discarding
successful patient results. A scheduled monitor marks stale open batches
`abandoned`; it must not guess that they completed.

### Patient lifecycle and optional request context

```http
PUT /api/v1/import-batches/{batch_id}/patients/{client_patient_key}/
POST /api/v1/import-batches/{batch_id}/patients/register/
POST /api/v1/import-batches/{batch_id}/patients/{client_patient_key}/attempts/
PATCH /api/v1/import-batches/{batch_id}/patients/{client_patient_key}/attempts/{attempt_id}/
```

At batch start, the ETL registers the frozen patient work list in bounded bulk
requests. This makes `not_started` and `missing` real rows instead of a number
inferred after a crash. Registration is idempotent and the server verifies that
the roster size equals `selected_patients` before normal completion. The ETL
then begins an attempt before resolving the person and closes it after the final
refresh/readback. This preserves failures that occur before a `Person` exists.

Add optional batch context to these existing writes:

- `POST /api/v1/persons/find_or_create/`;
- `PATCH /api/v1/persons/{person_id}/`;
- bulk/single condition, measurement, observation, procedure, and drug
  exposure creates and updates;
- patient refresh/derivation calls.

Use headers for cross-cutting context so bulk list bodies remain unchanged:

```text
X-Import-Batch-ID: <uuid>
X-Import-Patient-Key: <opaque key>
X-Import-Attempt-ID: <uuid>
```

The server validates that the batch is open, the attempt belongs to it, the
person belongs to the batch's organization (or is being assigned there by the
authorized create path), and every row in a bulk call names the same person.
Requests without these headers retain existing behavior, which keeps manual
edits and older importers compatible.

Where a write already creates a `ProvenanceRecord`, copy a nullable attempt
reference onto it. Record every write disposition in `ImportBatchWriteEvent`,
including updates, unchanged upserts, deletes, and rejections. This avoids
placing reporting columns on every OMOP table and makes counts server-auditable
rather than trusting client totals.

For fill-if-empty demographic updates, return field-level results
(`applied`, `unchanged`, `blocked_existing`, `invalid`). A 200 response that
applied none of the new source data is important quality evidence and should
not be reported merely as “patient updated.”

### Batch-aware source-code resolution

Extend `POST /api/v1/code-mappings/lookup/` rather than building a second
resolver. Accept optional batch/attempt context and, for each entry, a stable
event ID plus `occurrences` (default 1). Return the existing resolution payload
plus `mapping_id`, import-time resolution class, and whether this event was
newly recorded or replayed.

The existing response map is keyed only by `vocabulary|code`, so it cannot
represent the same code observed in two tables or repeated event IDs. Preserve
that map for compatibility and add a positionally aligned `results` array in
batch-aware mode. Each result echoes its event ID, vocabulary, code, and table;
new ETL code consumes this unambiguous array.

The ETL should call it for all coded facts and components before constructing
OMOP rows. It must retain the verbatim source system/code as source provenance,
write only an effective resolved destination, and use concept 0 for unresolved
facts according to current policy. It must not write a proposed target.

Resolution classes should distinguish at least:

- curator-approved;
- authoritative direct vocabulary match;
- proposed destination awaiting review;
- source vocabulary/concept not loaded;
- no candidate;
- rejected;
- uncoded source text;
- cross-domain conflict;
- invalid source code/system.

At batch completion, snapshot “resolved at import.” The UI should also join to
the current SCCM state so a batch can say “37 unresolved at import, 12 remain
today.” Never rewrite the historical snapshot when a curator later maps a
code.

### Read APIs

```http
GET /api/v1/orgs/{slug}/import-batches/
GET /api/v1/import-batches/{batch_id}/
GET /api/v1/import-batches/{batch_id}/patients/
GET /api/v1/import-batches/{batch_id}/codes/
GET /api/v1/import-batches/{batch_id}/issues/
```

Support pagination, status/date/flow filters, stable sorting, and server-side
search by safe identifiers. Code rows should default to:

```text
unresolved first,
then batch occurrences descending,
then affected patients descending,
then most recently seen,
then vocabulary and code
```

The UI should initially select unresolved rows and label the default sort
**Batch seen**. Offer **Affected patients** and **Global SCCM Seen** as explicit
alternative sorts. SCCM `Seen` currently counts unresolved resolver encounters,
whereas batch occurrences count all coded facts, so presenting either as an
unqualified `Seen` value would be misleading.

Return aggregate summaries separately from paginated rows. Avoid loading all
patient/code details as part of `OrgDetail`'s initial request.

## Import quality funnel

Per-patient and per-batch counters should form a loss funnel rather than one
undifferentiated “error count”:

```text
FHIR resources received
  -> resources supported by a handler
  -> clinical facts/components parsed
  -> facts valid enough to write (date/value/identity present)
  -> source codes effectively resolved
  -> rows submitted
  -> rows created / updated / unchanged
  -> rows reflected in the PatientRecord projection
```

Every drop between stages needs a bounded reason code. This separates the work
queues:

- unsupported resource type or unhandled field: parser coverage work;
- missing required date/value or invalid cardinality: source-quality or parser
  normalization work;
- unresolved source code: Code Mapping or vocabulary-loading work;
- API validation/constraint failure: PRomop contract work;
- written OMOP fact absent from `PatientRecord`: field-concept mapping or
  derivation work;
- source value offered but fill-if-empty rejected it: reconciliation policy
  work;
- duplicate/unchanged fact: expected idempotency, not failure.

Include the top reasons and counts in the batch summary, with a filtered
patient list behind each reason.

## Completeness measures

The following measures are more actionable than a single record-completeness
score and should be introduced after the core accounting is trustworthy:

### Identity and demographic coverage

- person resolved and assigned to the intended organization;
- name, full/partial birth date, sex/gender, race, ethnicity, and contact fields
  present when the source claims to provide them;
- conflicting existing demographics that prevented an update;
- source identifiers linked without exposing them in aggregate APIs.

### Clinical coverage

- conditions, measurements, drugs, procedures, observations, immunizations,
  allergies, encounters, documents, and episodes received versus represented;
- primary cancer diagnosis, diagnosis date, disease slug, stage, performance
  status, therapy history, current therapy, key disease labs, genomics, and
  treating institution by applicable disease profile;
- facts with dates, usable units, reference ranges, status, and provenance;
- newest source clinical timestamp and lag between that timestamp, batch time,
  and `PatientRecord.derived_at`.

### Terminology coverage

- distinct codes and occurrences resolved at import;
- distinct unresolved codes, affected patients, and occurrences;
- unresolved percentage by resource/domain and source vocabulary;
- concept-0 row count created by the batch;
- codes whose mapping changed after the batch and rows awaiting or completing
  repoint/refresh;
- mappings to local `HK-*` quarantine concepts versus standard concepts;
- retired destination concepts and source/domain conflicts.

### Operational health

- duration, patients/minute, API requests, retry attempts, and slowest stage;
- new versus existing patients and created/updated/unchanged/deleted rows;
- skipped and missing patients by reason;
- write rejection rate and errors grouped by stable fingerprint;
- comparison with the previous successful batch for the same org/flow,
  highlighting regressions in success, mapping, resource, and completeness
  rates rather than merely showing absolute totals.

The UI should label denominators. For example, “92% code occurrences resolved”
and “81% distinct codes resolved” answer different questions and both are
useful.

## Mapping-driven work queue

The source-code table is the main bridge from reporting to remediation. Each
row should display:

- mapping state and resolution class at import, plus current mapping state;
- source vocabulary, code, safe description, domain/table, and first/last seen;
- occurrences in this batch, distinct affected patients, and global SCCM
  `Seen` as separate values;
- destination concept and whether it is standard, local quarantine, or retired;
- `mapping_id` and the count of rows/persons likely to be repointed;
- an impact indicator based primarily on affected patients, then occurrences,
  recurrence across batches, recency, and whether the gap blocks a configured
  completeness dimension.

The default ordering should remain transparent; do not hide the underlying
counts behind an unexplained score. If an impact score is later added, publish
its versioned formula.

The **Map code** action should deep-link to `/code-mappings` with stable query
parameters for mapping ID or vocabulary/code and open the relevant row. The
Code Mapping page currently supports server-side browse/search but not a
durable incoming deep-link contract, so add and test one. After approval, the
operator should be able to return to the batch and see historical versus
current resolution without rerunning the import.

Provide aggregate actions only where existing governance remains intact:
“open unresolved codes in mapping queue” or “start suggestions” may navigate or
queue proposals, but batch reporting must never auto-approve mappings.

## Organization Stats experience

On `OrgDetail` > **Stats**, retain the disease table and add **Import Batches**
beneath it.

The batch list should show start/completion, flow/source, status, duration,
selected patients, good/partial/error counts, mapping coverage, and completeness
delta. Highlight an open batch whose heartbeat is stale and a completed batch
whose reconciliation equation does not balance.

Selecting a batch should open a dedicated route or drawer with:

1. headline patient outcomes and the reconciliation equation;
2. import funnel and top issue reasons;
3. source codes, defaulting to unresolved/impact order;
4. completeness before/after by dimension;
5. patient results and retry history;
6. technical metadata and safe links to Airflow/artifact diagnostics.

Use server pagination and lazy-load the detail sections. Preserve filter state
in the URL so an issue link is shareable. Display dates in the viewer's local
timezone while retaining ISO timestamps in the API.

Operational import detail should default to staff and administrators of the
owning organization. Do not automatically expose it through `OrgTrust` just
because aggregate disease counts include trusted organizations: patient-level
errors, source systems, and operational metadata are a different disclosure
surface. Add a separate permission only if analysts or trusted organizations
need it. Patient drill-down must also pass normal patient access checks.

## ETL integration plan

Add batch reporting at the shared seams so each flow does not reimplement it.

1. Extend `CtomopClient` with batch create/heartbeat/patient-attempt/complete
   methods and batch-aware SCCM lookup.
2. Add a small `ImportBatchReporter` interface with HTTP and no-op
   implementations. A no-op keeps local parser tests and deliberately
   unreported development runs usable.
3. Have the shared import runner open/close patient attempts and transfer its
   selected/imported/skipped/missing/error accounting. Flow adapters contribute
   safe metadata and counters only.
4. Have `FhirParsingService` expose the resource-to-row funnel and code
   observations instead of reconstructing them from written OMOP rows.
5. Pass batch context through `CtomopApiOmopWriter` into person, terminology,
   clinical write, and refresh calls.
6. Close a patient only after write and derivation/readback results are known.
   A process crash leaves an open attempt for the retry to supersede, rather
   than falsely marking it good.
7. Complete or fail the PRomop batch in a task-finalization path that runs even
   when `ImportRunReport.raise_if_failed()` makes the Airflow task red.

Do not make report delivery capable of turning a successful clinical write
into a duplicate on retry. Clinical writes and report events need independent
idempotency keys. If PRomop is temporarily unavailable before any clinical
write, retry normally; if reporting fails after writes, reconcile the same
batch/attempt rather than creating a new run.

## Privacy, retention, and observability

- Batch summaries and code aggregates contain no patient names, emails, MRNs,
  raw FHIR, access tokens, or unrestricted upstream error bodies.
- Uncoded free text can itself contain PHI. Apply length limits and a PHI-safe
  policy before retaining or displaying it; where safety is uncertain, store a
  fingerprint and restricted diagnostic reference rather than the text.
- Audit batch creation, closure, failure, patient-detail access, and links into
  curation. Reuse the existing audit event facility where possible.
- Retain batch aggregates and mapping observations long enough for trend work;
  use a shorter configurable retention for attempt errors and technical links.
  Decide and document the exact policy before production rollout.
- Add metrics for open/stale batches, report-event failures, reconciliation
  mismatches, patient outcome rates, mapping coverage, and API latency. Alert
  on stale batches and significant regression against the prior comparable
  successful batch, not on every expected unresolved code.
- Index batch list ordering, org/status/time filters, patient outcome, code
  resolution state, and mapping reference. Verify query plans on a realistically
  large batch before enabling the UI.

## Historical data and compatibility

Do not fabricate detailed batches from current `PatientRecord`, provenance, or
SCCM totals: those sources cannot reconstruct selection, retries, import-time
mapping state, or per-batch errors. Begin authoritative reporting when the ETL
adopts the contract. If a one-time baseline is useful, label it `derived
baseline`, omit unavailable metrics, and keep it visually distinct from real
batches.

All batch headers and patient associations are optional on existing person and
clinical endpoints. Existing callers continue to work. Once the ETL rollout is
stable, production ETL flows should require reporting through their own
configuration and fail preflight if they cannot open a batch; manual API use
remains optional.

## Delivery phases

### Phase 0 — contract and metric fixtures

- Freeze the outcome vocabulary, idempotency rules, safe error codes, source
  code normalization, and example API payloads.
- Capture representative result fixtures from FHIR, CureHub, HealthEx, and
  HealthTree flows, including failure before person resolution, partial bulk
  write, retry success, and an unresolved code.
- Audit SCCM code/domain collisions and decide its long-term identity before a
  schema change.
- Define the first two completeness profiles: a source-neutral core profile and
  one disease-specific pilot.

### Phase 1 — PRomop batch ledger

- Add models, constraints, indexes, permissions, serializers, batch lifecycle,
  patient-attempt endpoints, stale-batch handling, and read APIs.
- Add optional batch context to person and clinical writes and link provenance
  to attempts.
- Test idempotent replay, cross-org denial, invalid state transitions, server
  total reconciliation, redaction, and concurrent updates.
- Create `fhir_import_analysis_architecture.md` and document only the verified
  ledger and API behavior that ships.

### Phase 2 — terminology evidence and ETL adoption

- Make SCCM lookup batch-aware and idempotently aggregate code observations.
- Move the ETL FHIR writer from direct concept lookup to the governed resolver.
- Integrate the reporter into shared import and FHIR services; retain
  per-attempt results even when Airflow finishes red.
- Verify that proposed mappings are never written, retries do not inflate
  counts, and approval still repoints existing rows through the current
  governed path.
- Roll out to one ETL flow and organization, then the remaining FHIR flows.

### Phase 3 — Org Stats reporting and mapping links

- Add the lazy-loaded Import Batches list and batch detail experience.
- Add stable deep links into Code Mapping and return navigation.
- Add patient outcome, errors, skips, source-code impact, filters, empty/loading
  states, and accessible table behavior.
- Validate authorization for staff, owning-org admins, ordinary users, and
  trusted-but-not-owning organizations.

### Phase 4 — completeness and improvement loops

- Capture before/after completeness snapshots and the full resource-to-
  projection funnel.
- Add profile-aware trends and comparison with the prior comparable batch.
- Add actionable queues for parser coverage, source quality, API rejection,
  demographic conflict, stale derivation, and projection gaps.
- Measure whether mapping approval and parser changes improve subsequent
  batches; do not infer success solely from fewer errors.

### Phase 5 — hardening and expansion

- Load/performance test large batches and high-cardinality source-code sets.
- Apply retention, archival, monitoring, and abandoned-batch operations.
- Adopt the contract for non-FHIR imports where the same patient/code concepts
  apply, without forcing FHIR-only metrics onto them.
- Update `API_SURFACE.md`, ETL integration documentation, and operational
  runbooks; keep implemented behavior in the architecture document.

## Test strategy

### PRomop backend

- model constraints and lifecycle transition tests;
- create/complete/fail replay with idempotency keys;
- balanced and unbalanced selected/outcome totals;
- failure before a Person exists and retry after a partial attempt;
- optional batch headers on person, bulk create/upsert/update, delete, and
  refresh paths;
- organization authorization and non-leaking 404/403 behavior;
- provenance-to-attempt linkage and auditable server row counts;
- SCCM resolved/proposed/rejected/missing-vocabulary/direct cases;
- duplicate observation replay and concurrent counter updates;
- aggregate/read pagination, ordering, current-versus-import-time mapping, and
  query-count bounds;
- error/metadata redaction.

### ETL

- reporter contract tests with a fake client and a no-op implementation;
- run closes in success and in `raise_if_failed()` paths;
- process interruption leaves a recoverable open attempt;
- Airflow retry reuses the batch and creates a new attempt without double
  counting code events or clinical writes;
- FHIR resource/component funnel totals reconcile to writer outcomes;
- governed mapping results control concept IDs and proposed targets never do;
- report outage recovery does not duplicate patient rows;
- DAG and CLI use the same reporting service.

### Frontend

- batch list loading, empty, error, stale/open, and completed-with-errors states;
- headline reconciliation and good/partial/error semantics;
- code ordering, filters, historical/current status, and deep-link navigation;
- patient/error drill-down permissions and safe rendering;
- large result pagination and URL-restored filters;
- regression coverage for the existing disease Stats section.

### End to end

Run a fixture batch containing a new patient, an existing patient update, an
unchanged patient, a retryable failure, a benign skip, a missing item, a direct
mapping, an approved mapping, and an unresolved code. Verify the ETL artifact,
PRomop aggregates, OMOP/provenance rows, `PatientRecord` projection, Org Stats
UI, and Code Mapping link all agree.

## Acceptance gates

- [ ] A retried ETL run produces one batch, preserves each attempt, and does
      not double-count patients, code occurrences, SCCM `Seen`, or OMOP rows.
- [ ] `selected` reconciles exactly to terminal patient outcomes; open work is
      visible rather than counted as success.
- [ ] “Good,” “partial,” “error,” “skipped,” and “missing” have the same meaning
      in the ETL result, API, and UI.
- [ ] Every unresolved coded fact is retained with its source identity and
      batch impact, while no proposed destination is used as an OMOP concept.
- [ ] Unresolved codes default to batch-seen order, offer affected-patient and
      global-`Seen` sorts, and link to the exact Code Mapping work item; a later
      approval appears as current state without altering the import-time record.
- [ ] Patient creation/update and clinical writes accept optional batch context;
      callers without it remain compatible.
- [ ] Batch history is many-to-many and cannot be erased by a later patient
      import.
- [ ] The funnel distinguishes parser, source-quality, terminology, API, and
      derivation/projection gaps with bounded reason codes.
- [ ] Completeness reports state their applicable profile and denominator and
      show before/after evidence without exposing clinical values.
- [ ] Organization isolation, operational-detail permissions, PHI redaction,
      audit events, retention, and stale-batch handling are verified.
- [ ] The Org Stats page remains responsive on a realistic large batch using
      indexed, paginated, lazy-loaded queries.
- [ ] Implemented behavior is documented in
      `fhir_import_analysis_architecture.md`; this plan contains only remaining
      work, delivery status, dependencies, and acceptance gates.

## Explicit non-goals

- Storing raw FHIR bundles or unrestricted ETL logs in PRomop.
- Replacing Airflow's task/run monitoring with PRomop.
- Treating skipped or unchanged rows as failures.
- Auto-approving mappings or using machine proposals as clinical truth.
- Inferring trustworthy historical batches from current database state.
- Defining one universal completeness score for every disease and source.
- Exposing operational or patient-level import detail through organization
  trust relationships without a separate authorization decision.
