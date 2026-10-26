# Changelog

All notable changes to PRomop are documented here.

---

## [Unreleased]

### Added

- **Versioned clinical source-input SDK on dev** — opt-in, per-field approved
  source writes for laboratory ULN ratios, explicit swallowing answers and SCT
  dates append corrections/clear tombstones and verify refreshed readback.
  Three nullable projections add no medical defaults or mapping approvals.
  Existing INR/seconds and native SCT behavior remain distinct; see
  [the contract](docs/cb_clinical_inputs_architecture.md) and
  [rollout gates](docs/cb_clinical_inputs_plan.md).
- **A curator can walk the source codes again after loading patients** — the
  Concept to Code tab's Source-code coverage heading now carries the walk
  panel: when the last walk began, how many patients have source codes first
  seen since, and a button (approval rights) that records a rebuild request
  for the worker. While a walk is queued or running the panel shows its
  progress. (#2050)
- **The walk reports how much LOINC axis evidence the data holds** — a rebuild
  counts, over the codes it walks, how many have each axis settled and from
  what, how many carry only a hint, and how many have patients that disagree
  on it. The panel shows the table, so whether anything beyond units ever
  settles an axis is visible at a glance. (#2051)
- **The patient can confirm that the recorded treatment history is complete** —
  the Treatment tab now carries "This is my complete treatment history". Until it
  is ticked, the number of recorded lines is a lower bound ("at least N"); the
  projection exposes `therapy_history_complete` as null, and only an explicit
  confirmation makes it true. Confirming is a statement about the list, not a
  clinical fact: withdrawing it returns the record to the lower bound, and a
  patient copy carries the confirmation with the record. The write goes through
  the packaged endpoint, so an embedding host applies its own write gate and
  per-person authorization (#1924).
- **An ETL can write a patient's lines of therapy** — `POST
  /api/v1/therapy-lines/bulk_upsert/` takes one person's lines as the source
  states them (number, dates, ongoing, regimen label, outcome) and writes them
  through the canonical line writer in one all-or-nothing request, deriving the
  record once. Patients imported through the OMOP row endpoints had no lines,
  because none of those endpoints writes an `Episode`, and the per-line editor
  POST refuses the `system/etl.write` credential. Regimen labels resolve to a
  concept only through the curated regimen table or an exact HemOnc name;
  anything else keeps the label as the line's name and is queued as a regimen
  mapping gap, so an abbreviation such as "R" is never guessed (#1969).

- **A recorded line no longer has to name its drugs, and a wrong line can be
  withdrawn** — the therapy editor offers "I don't know which drugs were given",
  which records that the line happened with an unknown composition instead of
  refusing to save. That is a deliberate choice, not an empty form: a payload
  with no drugs, no regimen and no such statement is still rejected. Separately,
  each line has a Remove action that retracts the claim it happened; the line
  leaves the history and its imported drug exposures are kept, which is a
  different action from ticking "drugs unknown" on an existing line, where the
  line stays and only its composition is cleared. The write payload carries
  `composition_unknown` and `DELETE /v1/therapy-lines/<id>/` performs the
  retraction (#1924).

- **The patient-info widget ships with the therapy editor** — `npm run
  build:widget` produces the self-contained bundle a host on another React
  version mounts, and that bundle now includes the treatment editor, so a
  federated patient can author and correct lines instead of reading a
  read-only history. The build entry exposes `mount`/`unmount` with
  `{apiBase, token, readOnly}`, hands the base to the clinical transport and
  hides tabs whose endpoints only the standalone product provides. The
  treatment tab withholds its authoring controls when the host mounts
  read-only, instead of offering a write the server will refuse. Structured
  `lines_of_therapy` moved into `omop_core.api` so a host can project the same
  per-line payload the standalone record uses (#1924).

- **The therapy editor's endpoints ship in the package** — `omop_core.api`
  serves the five calls the patient widget makes (`therapy-lines`,
  `therapy-outcomes`, `therapy-regimens` list and detail, `concepts/search`)
  from the installed wheel, so an embedding host can run the editor instead of
  only deriving from data it cannot author. Host policy stays out: permissions,
  the per-person write check and the `PatientRecord` payload shape are seams a
  host overrides. The in-place API now imports the same serializers, queries and
  viewset, so the two surfaces cannot drift. Requires the `api` extra
  (`djangorestframework`) (#1924).

- **Download a patient record as PDF or FHIR** — the patient record view has a
  Download button beside the patient's name and number, for every role that can
  open the record (patients included; it used to be patient-only and FHIR-only).
  *PDF report* prints the tabs in order under their own labels, holding only
  fields with a value, plus therapy lines, supportive therapy, genomic findings
  and, for a patient's own copy, allergies and immunizations. It is generated in
  the browser from the loaded record, so it reflects unsaved edits and needs no
  new endpoint or dependency. Its layout is extracted from the tab components
  (`node scripts/tab-layout.mjs`), and a test fails when the generated layout is
  stale or misses a field the tabs render. *FHIR R4 bundle* now saves the signed
  export byte for byte, keeping its digest and signature valid (#1961).

- **Concept mapping priority** — on Code Mapping → Concept to Code, curators
  rank concepts by dragging rows, typing a priority (1 is first; clear it to
  unrank) or the up/down buttons. Ranked concepts come first on every page and
  under every filter; a page-size selector (50/100/250/500) and *Ranked only*
  bring concepts from later pages within reach. `POST
  /api/v1/concept-to-code/priority/` moves one concept and renumbers the list.
  Code-to-concept Suggest keeps a ranked concept that lexical search matched but
  cut, marks ranked candidates for the ranker as a tie-breaker between equally
  good matches, and notes when the winner is ranked. `copy_reference_data`
  carries the ranking by concept key (#1914).

- **Edit Mapping from Concept to Code, with example patients' Source
  Metadata** — every source code listed under a concept opens the Edit Mapping
  dialog (a code with no mapping row opens as a New Mapping), with the concept
  offered as the destination when the code has none. The dialog lists up to
  three patients whose source inventory carries the exact source system and
  code, and shows the chosen patient's Source Metadata
  (`GET /api/v1/concept-to-code/source-examples/`, administrators only, limited
  to patients the curator can open, read-only). After a Suggest run with a
  ranker, suggestions without a confidence score are hidden; unranked runs and
  the Existing mappings and Find source codes lists are unchanged (#1915, #1911).

### Changed

- **The deployed commit sits under the PRomop logo** — in tiny type, with
  the full SHA as a tooltip, so a report from staging names its build. It
  comes from the new public `GET /api/v1/version/`, which answers with
  Render's `RENDER_GIT_COMMIT` (or a checkout's `HEAD`) only when it is a Git
  commit (#2016).

- **LOINC axes read the record units first, and lead with the variants** —
  a code whose records all arrive in mass-concentration units showed every
  axis as "Not settled" when its stored metadata carried only a reference-range
  unit; record units now settle Property (when they agree) and Scale (whenever
  records carry units) ahead of the metadata's one sample. When the units
  imply several axis sets, the LOINC axes panel leads with the variants, their
  units and destinations, and folds the shared axes under them. The SNOMED
  evidence section is hidden on any LOINC axis evidence, not only a settled
  lab property (`snomed-attributes` answers `applies: false` with
  `loinc_evidence`) (#2014).

- **Suggest fills in a LOINC for each axis variant** — a source code whose
  units imply several LOINC axis sets no longer needs one Suggest click and
  one pick per variant. In Edit Mapping, Suggest also posts to
  `code-mappings/<id>/axis-variants/suggest/`, which suggests a standard LOINC
  for every variant without a destination under that variant's axes and
  stores it as proposed; the LOINC axes panel then shows each with Approve.
  The Concept to Code preview does the same for each flagged candidate and
  lists the variants, with their units and destinations, under the code, where
  an approver can approve them. The SNOMED evidence section is hidden when
  the source's evidence says it is a lab value, since its destination is a
  LOINC (`snomed-attributes` answers `applies: false` with `lab_signal`)
  (#2012).

- **The concept's source-code dialog opens on Suggested source codes** — and
  has two views instead of three: the Find source codes list is gone, since
  Suggest reaches the same rows with the ranker's evidence attached. Existing
  mappings is grouped by status: approved first and folded, then the proposals
  open for review, then rejected, folded, each group fetched and paged on its
  own (`status=` on the detail endpoint, with `status_counts` for the headers).
  Propose selected is gone too; Approve selected proposes a vocabulary-only
  candidate from its preview and approves it in the same click (#2010).

- **Mapping saves return immediately** — approving or re-pointing a code
  mapping (Concept to Code, Code to Concept, group approve, CSV upload) writes the mapping
  and returns; the stored clinical rows move to the new concept in a background
  job (`omop_core.repoint_mapping_rows`, Celery when `CELERY_BROKER_URL` is set,
  inline after commit otherwise). The destinations still to move are kept in
  `deferred_repoint_concept_ids`, so a lost or duplicate task loses nothing. The
  save response's `repoint` is now `{"queued": true, "from_concept_ids": [...]}`
  rather than row counts. The approval-time unit-mismatch confirmation (`409
  unit_mismatch_confirmation_required`, `confirm_unit_mismatch`) is removed:
  values are normalized when clinical rows are loaded from codes. A failed job
  is retried three times; `manage.py run_queued_repoints <id>…|--all` moves
  whatever is still queued. A CSV upload queues one job per approved mapping
  (#1952).

- **Clearer source-code curation evidence** — Edit Mapping now keeps Source
  Code Value beside Source Description, only loads publisher terminology for
  catalogs actually present, makes hospital/organization attribution explicit,
  omits redundant coding counts, explains value types outside the three extract
  categories, and labels grouped section totals and same-description record
  counts explicitly (#1691).

### Fixed

- **A refresh whose worker was killed is picked up without a deploy** — the
  stale-heartbeat sweep ran at worker boot and twice in the next three minutes,
  then never again. Cloud Run shuts the old instance down after the new worker
  is up, so a walk killed that way went stale after the last recheck and stayed
  `running` until the next deploy; the redelivered message found a row that was
  not `queued` and skipped it. A live worker now sweeps every five minutes, so
  a stranded request is requeued and resumed within about one stale interval.
  (#2048)
- **Unit counts come from the instance's own patients** — migration 0307 asks
  the worker to rewrite every code's stored unit evidence from the source codes
  on its loaded patients and the stored measurements, then refresh its axis
  variants, through the same durable request as the variant walk. The seeded
  label-level counts (#2041) are replaced, a variant the evidence no longer
  shows is deleted unless a curator decided it, and a later seed or inventory
  load leaves patient-derived evidence alone. `manage.py refresh_axis_variants
  --rebuild-evidence` does the same by hand. (#2043)
- **Unit counts in the axis variants table are the code's own** — the
  hospital code seed took each unit's count from the inventory CSV, whose unit
  rows are a label-level join repeated for every code with the label, so every
  albumin code of a hospital showed the same `g/dL 10, % 1`. The seed build now
  takes the per-code count, patients and values from the v2 extract and leaves
  out a unit the CSV knows only for the label, so nothing reads it as the
  code's evidence. Instances seeded before this need the seed rebuilt and
  reloaded. (#2041)
- **A variant's suggestion candidates say how to use them** — the blue
  candidates area names the variant it is for ("Suggestion candidates for the
  % variant"), every row has a **Use** button (the winner's reads **Use
  winner**), the variant's current destination is marked, and the confirmation
  appears with the variants. The variant row's search button reads **Search
  destination**, so it is not mistaken for applying the suggestion. (#2036)
- **The "Axes shared by every variant" fold is gone** — on every instance
  measured it settled nothing but Scale, which each variant row shows, and no
  curator had ever entered a shared axis; the six-axis table stays for a code
  with one axis set. Saved entries still reach Suggest. (#2037)
- **Concept to Code Suggest moves on instead of repeating declined codes** — a
  run used to retrieve the same best-`limit` sources every time, so when the
  ranker declined all of them ("25 without a confidence score hidden") the next
  click found nothing new. Declines are now remembered per concept and skipped
  by later runs and by the estimate, which says how many are skipped; "Retry
  declined" searches them again. The Suggest button sits on the same line as
  the ranker and the limit, and the run summary says the hidden codes were
  declined and that the next run moves on. (#2034)
- **A `%` variant is no longer suggested the mass-concentration LOINC** — a
  unit that fits several LOINC properties (`%`, `U/L`, `IU/L`, `{ratio}`) now
  settles the property to that set instead of none, so albumin in `%` keeps
  the fraction codes and excludes 1751-7 the way the `mmol/L` variant already
  did. The sets come from the properties of the LOINC codes that list the unit
  among their example units; `mIU/L`, `mIU/mL` and `g/g` turned out to name one
  property each and now settle it; on a loaded instance the sets are read from
  the LOINC table's example units, so a new release is followed. Units with the
  same set (`U/L`, `IU/L`) are one variant. Suggesting a variant reads the axis
  values its units imply now, not the ones stored before this change, and a
  variant stored under the older per-unit key is re-keyed, keeping its
  destination. The dialog shows a set as "any of …". (#2029)
- **A running Suggest no longer locks the Concept to Code list** — opening a
  source code to edit it, approving a variant in place, folding a group and
  paging wait only for a write in progress; the search's own controls and the
  batch selection still wait for the run. An edit saved while the run streams
  is kept when the next poll replaces the run's candidates. (#2028)
- **The Edit Mapping dialog says it is loading the LOINC axes** — the section
  is on screen as soon as the dialog opens, with a status line that names how
  many axis values the queue flagged, instead of appearing without warning once
  the unit evidence has been scanned. (#2030)
- **A deploy mid-walk no longer costs the axis variant refresh six hours** —
  the request row now carries a `progress_at` heartbeat written after every
  batch. A `running` request with no heartbeat for 15 minutes lost its worker
  to the deploy that replaced the pool, and the next worker boot requeues it
  and resumes at its `done` offset instead of waiting for the six-hour task
  limit and starting over. (#2023)
- **The commit stamp is filled in on HealthTree GCP** — the Cloud Run image
  now carries `GIT_COMMIT`, passed by the deploy workflow as a build argument,
  so `/api/v1/version/` and the line under the logo show the deployed commit
  there as well as on Render. (#2024)

- **The bulk LOINC axis variant backfill no longer runs inside `migrate`** —
  migrations 0300 and 0301 walked every multi-unit code during the release
  step. On the HealthTree GCP instance, which holds the data with the most such
  codes, the walk outlived the Cloud Run migrate job's one-hour task limit on
  every attempt, so no deploy reached traffic between 21 and 24 October. Both
  migrations are now no-ops and migration 0303 records an `axis_variant_refresh`
  request that the Celery worker performs (`omop_core.refresh_axis_variants`,
  six-hour limit, progress on the row). A worker publishes queued requests when
  it starts, so a rolling deploy cannot lose one, and `manage.py
  refresh_axis_variants --queue` records a request by hand. (#2021)

- **A known line of therapy is no longer lost when its drugs are unknown** —
  the canonical writer now records that a line happened, independently of the
  exposures that describe it. A line named without a drug list (a mapped
  regimen, or a patient-reported line whose drugs were never recorded) counts
  as one prior line instead of reading as "no prior therapy", and reports an
  unknown composition rather than an empty one: the per-line payload now says
  `composition_known: false`. A header row that no writer asserted is still
  ignored, so a partial import cannot invent treatment. Retracting a line
  entered in error withdraws it without deleting imported exposures, and saving
  the line again restores it; clearing composition keeps the line with unknown
  drugs (#1924).

- **Editing a line of therapy no longer rewrites unrelated administrations** —
  every drug exposure now belongs to one line and is addressed by its own id,
  with its own dates, instead of being re-identified by patient, drug name and
  date. Correcting a drug or its dates updates only the exposure the line owns;
  exposures shared with another line or imported from an EHR are copied before
  an edit and never retracted. Saving the form again, or editing only the
  outcome, keeps the existing ids and individual administration periods, and a
  line's dates no longer overwrite them. Repeated administrations of the same
  drug are preserved, an ambiguous legacy name is refused instead of guessed,
  and a future-dated administration is refused because the history records
  treatment received rather than planned. The write is one transaction with a
  bounded retry on lock contention, so a concurrent bulk edit cannot leave a
  partially written line; ARTEMIS materialization uses the same boundary
  (#1926).

### Added

- **Source evidence in Edit Mapping** — the dialog now shows the hospital
  organization (or explicit unattributed state), source record/patient counts,
  raw units, supplied per-unit percentiles, and source reference-range evidence.
  Source retirement and its OMOP concept id are compact inline annotations;
  manual UMLS lookup is left to Suggest. Hospital-code imports retain the v2
  evidence and use its deduplicated label count so sibling Epic codings on the
  same observations do not inflate grouped Seen (#1688).

- **Epic/Cerner hospital-code inventory backfill** — an idempotent management
  command unions the deletion-cleaned HealthTree Parquet inventory with
  supplement-only keys from the earlier CSV, preserves exact vendor tenant
  systems, and attaches raw per-code unit evidence to approval warnings. This
  populates the data-driven Epic and Cerner Code Mapping tabs without treating
  ingestion-channel labels as hospital organizations (#1683).

- **Asynchronous Athena setup and refresh** — deployment preparation now queues
  durable Athena work instead of downloading and scanning the vocabulary in
  Render pre-deploy. New instances and changed Drive artifacts load on the
  dedicated Celery `athena` queue with deduplicated queued/running state,
  observable success/failure receipts, retryable failures, and a task/visibility
  window sized for the full initial release (#1674).

- **Athena freshness and scoped deploy deltas** — the Athena sync compares the
  selected artifact in the governed Google Drive folder with a durable local
  receipt. An unchanged Drive file exits before downloading the archive or
  touching vocabulary tables; a changed artifact is SHA-256 fingerprinted and
  passed through the existing insert-only loader under a database advisory lock
  and transaction. Only absent rows commit, failures roll back to the previous
  published release, and check/dry-run/apply modes record structured outcomes
  and per-table delta counts (#1652).

- **Code Mapping CSV upload** — mapping users can upload source-code
  inventories from the Code Mapping page with a source vocabulary and editable
  provenance (defaulting to their email). The atomic importer accepts `source
  code`, `source description`, and optional `seen count`, preserves existing
  curation decisions, reports row-level validation errors, and deduplicates
  identical artifacts so encounter counts cannot be applied twice (#1656).

- **LOINC loads itself from loinc.org** — `sync_loinc_release` checks
  `GET /api/v1/Loinc` for the published version and, when ours is older, streams
  the release archive and loads both tables from it: `LoincTable/Loinc.csv` into
  `LoincCodeClass` (including the `property` and `scale_type` that migration
  0249 added and no load had ever filled), and `AccessoryFiles/PartFile/Part.csv`
  into `LoincClass` — LOINC models CLASS as a Part, which is why the archive has
  no file named after classes. The loaded release is recorded in `LoincRelease`,
  so "is our copy stale" finally has an answer. The deployment bootstrap runs
  the check and queues the load on Celery; a hand-run Cloud Run job, a one-off
  GCS bucket and a dependency on an hk-labs checkout all go away. Needs
  `LOINC_USER` / `LOINC_PASSWORD` (#1624).


- **Label rollup for the Code Mapping queue** — a "Group by label" toggle shows
  one review row per label instead of one per vendor code, ordered by the
  group's summed Seen and paginated by group. Expanding a row fetches its codes
  from `GET /v1/code-mappings/group/`, which renders them exactly as queue rows.
  A group names a destination only when every member agrees. Off by default;
  the flat queue is unchanged (#1571).

- **Label grouping key for code mappings** — `SourceCodeConceptMapping` gains
  `source_label_norm`, a database-generated normalisation of
  `source_code_description`. Vendor-local codes are opaque (Epic flowsheet ids,
  Cerner `codeSet/72` numbers) but their display text is not, and the same text
  arrives under thousands of codes: in a 721,620-code HealthTree extract
  `albumin` appeared under 2,557. The row key stays
  `(source_vocabulary_id, source_code)` and ingest resolution is unchanged;
  this is a grouping key, so one review row and one ranker call can stand for a
  label instead of a code. NULL means the row belongs to no group — the label
  is absent, blank, punctuation only, or stored at the column limit and so
  possibly truncated (#1571).

### Changed

- **Security review is scoped to security-sensitive GitHub controls** — ordinary
  CI/test workflows and repository automation under `.github/` no longer need
  an automatic Lars review request or independent writer approval solely because
  of their directory. CODEOWNERS and the status policy still protect security
  policy, deployment, release, publishing, CodeQL, custom-action, ownership, and
  dependency-update controls (#1681).

- **Faster backend CI provisioning** — ordinary pytest runs no longer download
  Chromium or install its operating-system libraries. A separate path-gated
  smoke job performs the real browser install and launch when Playwright or
  deployment packaging changes. Backend and async jobs use a cached, pinned
  `uv` installer. Pytest retains its faster file-grouped xdist scheduler and
  slow-test report after CI measurement showed work stealing was slower for
  this database-heavy suite (#1678).

- **Render staging prepares once per deploy** — the staging web service gains a
  `preDeployCommand` running `scripts/prepare-deployment.sh`, the release phase
  Cloud Run already has via its gated `<service>-migrate` job. A failure now
  stops the deploy and leaves the running version serving, instead of producing
  a crash-looping instance. `start.sh` still prepares on boot as well until a
  Blueprint sync is confirmed to have applied the hook — the script is
  idempotent, so the second run is a no-op. Production waits until `dev`
  reaches `main`: a Blueprint sync applies settings to every service it
  declares, whatever branch each deploys from, so pointing production at a
  script only `dev` has failed its deploys with status 127 (#1625).

- **Deployment preparation is stated once** — the checks, migration and
  vocabulary bootstrap a deployment must complete before serving now live in
  `scripts/prepare-deployment.sh`, which `start.sh` invokes. Render's sequence is
  unchanged; the point is that Cloud Run's `<service>-migrate` job can run the
  same steps instead of its own copy defined outside this repository (#1625).

- **Code Mapping queue order** — every section now defaults to Seen descending.
  Unmapped grouped by Provenance first; that is a no-op on a tab holding one
  provenance, and on a tab holding several it buried the rows a Suggest run had
  just answered, because a run rewrites `origin_system` from `''` to
  `suggest v0.4`, which sorts last ascending. Provenance stays sortable and
  gains a filter control, which finds curator-edited rows in one click however
  many there are (#1575).

- **Copying between instances** — models are classified as system, reference or
  patient data (`omop_core/services/instance_data.py`). `copy_curation` is
  renamed `copy_reference_data` and now also copies lookups, therapy reference data,
  mapping destination candidates and HealthKey concepts.
  New `copy_patient` copies patients, by id or `--filter-org-id`, with every
  link remapped.

## [1.3.0] — 2026-09-16

251 commits since 1.2.0. The defining changes are genomics, federation, and
code mapping intelligence: cytogenetic and molecular markers land as structured
OMOP facts, the patient UI ships as a Module Federation remote, and Suggest
learns three retrieval strategies with accuracy tracking.

### Added

- **Genomics tab** — structured entry for cytogenetic markers (del(17p),
  del(11q), t(4;14), t(14;16), gain(1q), etc.), TP53 disruption status, clone
  fraction, and molecular findings. Values stored as coded OMOP Observations
  with LOINC concepts; derived markers projected onto PatientRecord for
  eligibility matching (#1166, #1208, #1209, #1249).
- **`populate_genomics_sample_data`** management command seeds realistic
  genomics profiles on sample patients (#1213).
- **Module Federation** — patient info and lab results exposed as federation
  remotes (`PatientInfo`, `LabResults` bridges) for the ht-phr host app.
  Federated patient view includes History tab and conditionally-shown Disease
  tab (#1114, #1362, #1364).
- **OMOP pages through clinical transport** — federated views fetch paginated
  OMOP data through the clinical transport layer (#1366).
- **History tab** — Summary tab renamed to History; trend chart icons added to
  Labs tab. Concept display names exposed on History tab endpoints (#1343,
  #1355).
- **Clinical Summary tab** — read-only clinical summary view (#1192).
- **Disease selector on General tab** — disease selector moved from standalone
  control to General tab; Disease tab shown conditionally based on selection
  (#1348).
- **Multi-strategy Suggest** — code mapping suggestions use a three-tier
  waterfall: UMLS CUI bridge, lexical trigram retrieval, and vector reranking.
  Per-code cost dropped from 17.8s to 3.5s. Runs queued on Celery with
  progress polling; inline fallback for deployments without Redis (#1081,
  #1093).
- **Suggest accuracy dashboard** — tracks suggestion acceptance rates by model
  version and source vocabulary, with per-tab and overall metrics (#1058,
  #6404).
- **UMLS source enrichment** — source code mappings enriched with UMLS
  preferred names and CUI metadata for curator context (#12e7ee2).
- **Bulk OMOP updates and deletes** — `PATCH /bulk_update/` and
  `POST /bulk_delete/` on the five clinical endpoints for batch corrections
  and reconciliation (#e4439074).
- **ICD-10 tab consolidation** — ICD-10-CM merged into the ICD-10 tab with
  UMLS lookup and SNOMED candidate concept resolution (#1028, #1042, #1044).
- **Treatment tab overhaul** — PatientRecord-first writes, complete treatment
  editors with supportive courses, dated history, and line-of-therapy tracking
  (#1139, #1160).
- **Trial favorites and saved filters** — per-user trial favorites, registered
  interest, and saved filter persistence (#1142).
- **Conditional preferences writes** — `If-Match` / `If-Unmodified-Since` on
  preferences PATCH to prevent lost updates (#1312).
- **Patient list enrichment** — clinical context badges, audience views,
  genomics and therapy counts, and sample stage population (#1234, #1254).
- **Code mapping copy between instances** — `copy_curation --tables
  code_mappings` copies `SourceCodeConceptMapping` rows between PRomop
  deployments, re-resolving concept FKs by vocabulary and code (#f7593832).
- **LOINC unit display in code mappings** — `measurement_type` and
  `suggested_unit` shown on mapping rows, destination options, and as a
  dedicated "Unit" field in the Destination panel. Broad coverage via
  `LoincCodeClass.example_units` from `Loinc.csv` (~41k codes with units)
  (#1376, #1381, #1382).
- **Semantic retrieval for mappings** — context-grounded search expansion using
  embeddings for mapping suggestions (#1164).
- **Sentry error monitoring** behind `SENTRY_DSN` environment variable.
- **PROlog survey runner** — PROlog integrated as a Django app (v0.1.0 through
  v0.4.2), replacing the project's own survey feature. Survey responses bound
  to PRomop-minted persons; runner frontend served when mounted (#1328).
- **ARTEMIS episode materialization** — adapter for episode artifact generation
  with source interval validation and stale artifact rejection.
- **Destination count and Seen count columns** in Code Mappings table (#1080).
- **PRomop branding** — logo on every page, administration titles, browser tab
  title and favicon (#1262, #1264, #1287, #1305).
- **HealthTree CSV occurrence frequencies** loaded into Seen counts (#1105).
- **Patient record attestation** and admin-gated field visibility (#1165).
- **Vocabulary content checksums** — verifiable checksums published on
  vocabulary releases (#1222).

### Changed

- **`promop` is canonical** — project renamed from `ctomop` with deployment
  compatibility retained (#1294).
- **Browser auth switched from OAuth tokens to sessions** — eliminates token
  refresh races and simplifies the frontend auth flow (#1216).
- **Refresh performance** — linear scans converted to O(1) index lookups in
  `refresh_patient_record`, reducing derivation cost on large patients (#1248).
- **Profile writes unified** through PatientRecord PATCH, skipping the OMOP
  read-back for direct edits (#1159, #1163).
- **Security controls decoupled from DEBUG** — production security posture no
  longer tied to the debug flag (#1211).
- **CI optimized** — backend suites run concurrently, skipped for
  frontend-only changes, and documentation-only changes skip application
  suites entirely (#1270, #1291, #1320).
- **Legacy `/api/` sunset extended** to 2026-12-01 (#271).
- **Python 3.12 required** for Render deploys and local development.
- **Date of birth correctable** via person endpoint.

### Fixed

- **FHIR and CSV uploads restored** for organization admins and staff (#1261).
- **Scoped Org Admins** can edit patient records (#1252).
- **Sensitive profile fields** gated behind staff access (#1206).
- **Password reset** targets active local identities only (#1302).
- **HTTPS-only OAuth redirect** boundary enforced as documented (#1339).
- **SMART scopes** enforced for shared service tokens (#1085).
- **Service identity security** — staff-managed application tokens (#1218).
- **Breast cancer question codes** corrected for Ki-67, methodology and nodal
  status (#1227).
- **TP53 disease prevalence** matched by substring instead of exact name
  (#1357); `tp53_disruption` returns `False` for absent status instead of
  `None` (#1359).
- **ANC and platelet projection units** normalized (#1289).
- **Render worker** can run any management command without HTTP config (#1338).
- **Cytogenetic multiselect** — each value mapped to its own Observation
  (#1208).
- **Startup contract** — `prepare_production_database` replaces retired concept
  seeder, bounded vocabulary bootstrap (#1124).
- **Mapping dialog** — retired concept metadata, destination search by OMOP
  concept ID, UMLS check, curated source value fallback (#968, #970).
- **Suggest** — multi-CUI overflow, embedding resilience to connection drops,
  snapshot-based recomputation skip, suggestion provenance versioning.
- Multiple migration graph conflicts resolved across concurrent branches.

### Migration notes

| Item | Details |
|------|---------|
| Migration endpoint | `omop_core.0238_loinc_code_class_example_units` |
| Total migrations | 239 (up from 202 in v1.2.0) |
| Post-migrate step | Run `load_loinc_classes` with a LOINC archive to populate `example_units` |
| Breaking changes | None — all additions are backwards-compatible |

### Known issues

- `{beat}/min` unit display strips braced text in the frontend (#1383) —
  display-only, data is correct.

---

## [1.0.0] — 2026-07-04

First stable release. Deployed in production across the HealthTree Foundation and CancerBot,
supporting approximately 17,500 oncology patients and trial matching against 6,000 actively
recruiting trials.

### Core architecture
- OMOP CDM v5.4 PostgreSQL schema as the authoritative clinical record
- `PatientRecord` — 286-column denormalized projection auto-derived via Django signal chain on every OMOP write; reduces a 20-criterion eligibility search from 27–39 joins over raw OMOP to zero
- FHIR R4 Bundle ingestion (`upload_fhir` endpoint + `import_fhir_bundle` management command) mapping observations → `Measurement`, conditions → `ConditionOccurrence`, medications → `DrugExposure` + `Episode`
- Line-of-therapy inference from `DrugExposure` / `Episode` records (ARTEMIS-derived heuristics)

### API
- Versioned REST API at `/api/v1/` with OpenAPI 3.0 schema (`/api/v1/schema/`) and Swagger UI (`/api/v1/docs/`)
- Legacy `/api/` paths retained with `Deprecation` / `Sunset` / `Link` headers (sunset: 2026-09-01)
- `GET /api/v1/patient-records/me/` — patient self-service record access, guarded against auto-provisioning clinical users

### Multi-tenant access control
- Organization model with role-based `GroupAccess` (org admin, doctor, navigator)
- Org invitation flow with email (Mailgun) and OIDC support
- Inter-org trust rules (`OrgTrust`) and public aggregated org statistics
- Service-token ACL bypass for machine-to-machine integrations
- SMART on FHIR authorization + OAuth2 (django-oauth-toolkit)

### Disease-specific extensions (oncology)
- Multiple myeloma: ISS/R-ISS staging, beta-2 microglobulin, FISH cytogenetics, stem cell transplant history/eligibility, therapy line tracking
- Follicular lymphoma: FLIPI, bone marrow involvement, transformation status; HemOnc concept-driven FHIR generator
- Breast cancer: ER/PR/HER2 status, HER2 IHC/ISH, Ki-67, TNM staging
- CLL: CLL-IPI, IGHV mutation status, del(17p)/del(11q)
- Wearable summary fields (step count, resting heart rate, sleep, HRV, SpO2)

### Developer experience
- `generate_fhir_bundle --disease {breast-cancer|mm|fl}` — reproducible synthetic FHIR patient generator
- `import_fhir_bundle` — batch FHIR import with `--org`, `--batch-size`, `--start-from` (resume support)
- Audit log middleware — structured JSON log for every mutating API request
- 640+ backend tests; CI on GitHub Actions (PostgreSQL 16)

## [1.2.0] — 2026-09-04

501 commits since 1.1.0. The defining changes are vocabulary governance and a writable
clinical surface: Athena becomes a first-class dependency, source-code-to-concept
mappings gain a reviewable approval pipeline, and the UI can write clinical facts back
into OMOP — not just display a derived projection.

### Upgrade requirement — load the Athena vocabularies before deploying

```bash
python manage.py load_athena_vocabularies --path /path/to/athena --concepts-only
```

Clinical foreign keys resolve against loaded vocabulary. **Without this load** the
writable-field descriptor reports no editable fields, demographic corrections save
as text with a null concept, and derivation silently reads nothing.

`--concepts-only` skips `concept_relationship`, `concept_ancestor`,
`concept_synonym` and `drug_strength` — the difference between an 11-second load
and streaming ~26M rows to add a handful of concepts.

Newly in `VOCAB_SCOPE`, all of which were in the Athena bundle all along and
simply never loaded:

- **`Gender`, `Race`, `Ethnicity`** — `Person.race_concept` could not previously
  hold a real concept on any deployment.
- **`Episode`, `CDM`** — `32531` Treatment Regimen (what line-of-therapy episodes
  point at) and `1147094` `drug_exposure.drug_exposure_id` (referenced by
  `EpisodeEvent`). Both were hand-seeded; Athena supplies them with identical ids,
  names and codes.

### Removed

- **`seed_omop_concepts` is removed.** It maintained 99 concepts by hand, 97 of
  which Athena already supplies. Offering it as an operator command made it a
  competing source of truth for concepts, which is how a locally invented concept
  ends up occupying an id the vocabulary owns — what happened with `3000963`,
  turning every unmapped lab into a haemoglobin result and leaving 19 staging
  patients with a haemoglobin of 1.0 g/dL. The data survives as
  `omop_core/concept_fixtures.py`, imported only by tests that need concepts
  without a 4.6 GB Athena bundle. Locally-minted `HK-Wearable` concepts arrive
  via migration 0143.

### Added

- **Source code concept mapping (SCCM) framework** — a reviewable, approvable
  pipeline for mapping source codes to standard OMOP concepts. Doctors and
  analysts propose mappings; administrators approve them. Unapproved mappings do
  not enter the clinical record (#820, #830, #834, #848, #849, #856, #872, #875).
- **Code mapping administration hub** — visual interface with approval queues,
  role-based gates, source vocabulary tabs (Athena sync, CR mirroring), batch
  ETL crossmap import, and mapping coverage statistics (#894, #896, #898).
- **Multiple source vocabulary imports** — UMLS/MRCONSO direct import with
  streaming loads (#983), HK-Labs curated LOINC mappings auto-approved on deploy
  (#941), HealthTree FHIR crossmaps (#891, #912), CureHub FHIR crossmaps (#924),
  and OpenWearables vocabulary (#909).
- **Batch concept lookup** — `GET /api/concepts/lookup/` resolves vocabulary + code
  pairs in bulk, so an ETL pipeline does not need one round-trip per code.
- **Auto-suggest mappings** — unmapped fields get suggested mappings based on name
  similarity, concept domain, and vocabulary context (#682, #856).
- **Field mapping transfer** — `copy_curation` management command copies
  curated mappings between PRomop instances. Matching is by natural key, concept
  FKs are re-resolved by `(vocabulary_id, concept_code)`, and reviewer attribution
  is cleared (#981).
- **UMLS and vocabulary release management** — release caching and pinning (#980),
  direct vocabulary release publishing, nested archive support, and streaming UMLS
  imports without full-archive caching.
- **Writable-field descriptor endpoint** — clients query which fields are writable,
  what concepts they accept, and how to write them as OMOP facts (#605).
- **Writable demographics** — Gender, Race, and Ethnicity are now correctable with
  coded OMOP concepts, not free text. Six location fields updatable through the
  Person endpoint (#608, #609).
- **Labs tab writes OMOP Measurement facts** directly, with concept resolution and
  unit normalization, driven by the writable-field descriptor (#602).
- **Blood tab writes OMOP facts** for hematology values through the same
  descriptor-driven pipeline (#622, #955).
- **Disease, Behavior, and Wearable tabs** rendered from the field descriptor, so
  new fields appear in the UI without frontend changes (#645, #647, #649, #651).
- **Treatment tab** — clinicians author lines of therapy through the API with
  structured regimen references, drug class categorization, and a disease-filtered
  regimen picker (#637, #639, #641, #672).
- **Therapy reference tables** — curated CSV-seeded therapy and component reference
  data, regimen picker filtered by disease, drug class categorization, and
  line-of-therapy metadata (#763–#767, #775, #776).
- **Custom patient fields** — foundation for organization-specific patient
  attributes, so each deployment can extend the record without forking the
  schema (#727).
- **Field mapping enhancements** — concept mapping interface for PatientRecord
  fields (#595, #616), tabbed layout with synonym management (#624), compound
  field mappings (#674), curated field mapping units (#689, #704), field formulas
  with derivation (#675).
- **Async derivation with Celery** — `?skip_refresh=true` suppresses PatientRecord
  rebuild on bulk POST and row-level PATCH/DELETE; `POST
  /api/v1/patient-records/{person_id}/refresh/` queues derivation on Celery,
  returns `202 Accepted` with a task ID. Inline dispatcher for dev without Redis.
  25-second statement timeout on both paths. Signed inline task IDs. Status
  polling at `/api/v1/derivation-status/{task_id}/` (#678).
- **FHIR DocumentReference ingestion** — clinical documents attached to a patient
  record are captured in OMOP (#569).
- **FHIR observation ranges and interpretation** — reference ranges and abnormal
  flags from the source system are mapped and stored (#562).
- **FHIR observation deduplication by concept** — duplicate observations from
  multi-provider patients are collapsed rather than stacked (#561).
- **Skipped FHIR resource reporting** — resources that cannot be ingested are
  logged with the reason, creating an audit trail for data completeness (#570).
- **Clinical OMOP list pagination** on the five clinical endpoints (#564).
- **Clinical list filters** for conditions, measurements, observations, drug
  exposures, and procedures (#565).
- **Single-POST upsert** — row-level clinical POSTs now upsert on event identity,
  matching the bulk path (#567).
- **Clinical provenance idempotency** — re-synced clinical writes do not duplicate
  provenance records (#558).
- **Apple/Garmin wearable code mappings** — curated mappings managed through the
  SCCM approval workflow, with database-driven ingest configuration (#923, #951).
- **Language skills** — a patient's language capabilities (speak, read, write, sign)
  are settable from the API and UI, coded with HK-Language concepts, and flattened
  for matching (#808, #813, #821).
- **Organization-scoped signup** — patients can self-register and associate with an
  organization through an email-filtered invitation flow (#572, signup filters).
- **Pending organization invitations** visible on user profile.
- **Sign Up tab** on the homepage login page (#572).
- **Vocabulary load from Google Drive** — `load_athena_vocabularies --gdrive` for
  deployments that store the Athena bundle on GDrive (#631).
- **Validation fields modelled as person equivalences** (#783).
- **`load_mappings` command** — integrated with `load_athena_vocabularies` for
  deploying mapping artifacts alongside vocabulary loads (#977).
- **HK-Labs SCCM seed migration** — migration `0201` seeds approved HK-Labs-to-LOINC
  mappings; deployment gate in `start.sh` enforces Athena load before applying (#972).
- **Tab-field overlap ESLint rule** — prevents a field from rendering an editable
  box on two tabs; replaces the runtime test (#955).

### Changed

- **Value concepts coded, not just questions** — observation and measurement
  answers resolve to coded concepts, not just source text (#774, #723).
- **Refresh prefetch** — OMOP rows prefetched to eliminate 504s on large patients
  during PatientRecord derivation (#541).
- **Refresh snapshot reuse** — LOT inference reuses the refresh snapshot instead of
  re-querying (#617).
- **Concept-zero repointing** — unambiguous concept-zero clinical rows safely
  repointed to the correct concept, gated on source provenance (#846).
- **Destructive vocabulary replacement blocked** when patient data references
  the vocabulary (#681).
- React pinned to 19.2.6 (#pin-react-19.2.6).
- DRF bumped from 3.15.2 to 3.17.2 for CVE fixes.
- Tailwind preflight scoped to stop leaking into the federation host document.
- Theme vars no longer written as inline styles on the root element.
- Docker stack made configurable and able to start with `DEBUG` off (#553).
- Fixed database-name portability: removed hardcoded `ctomop` assumptions (#551).

### Fixed

- **Unmapped-lab concept collision** — stopped the unmapped-lab fallback from
  occupying a real concept's id, which turned every unmapped lab into a false
  haemoglobin result (#599).
- **BMI derivation** for metre-height inputs (#769).
- **Regimen naming** — stopped a combination from being named after one of its
  drugs (#642).
- **Upsert timezone normalization** — upsert key datetimes normalized to avoid
  timezone-aware/naive mismatches creating duplicates (#531, #533).
- **Superseded-row upsert match** — a superseded row is no longer matched by a
  later identical write (#649 followup).
- **Projection patch** — sending an edit no longer drops derived fields from the
  save payload (#627).
- **Domain box overflow** — domain box value no longer bleeds into adjacent fields
  in the code mapping UI (#928).
- **Org cascade** — `manage_language_skills` stopped minting concepts and matching
  by name (#812).
- **Audit log** — deprecation warnings use a separate logger to avoid polluting
  the structured audit log (#879).
- **Clinical session auth** enforced by role (#555).
- **Clinical query filter validation** — unknown filters rejected with 400 (#563).
- **CORS provenance headers** — `X-Provenance-Source` and `X-Provenance-User-Id`
  allowed through CORS (#554).
- **M-Protein type values** updated (#811).
- **CLL/DLBCL disease string variants** handled in the regimen picker (#798).
- **Frontend lint regressions** — `set-state-in-effect` violations fixed in the
  field-mapping load (#630) and synonym dialog (#638).
- **P0 security audit findings** closed (#756): token cache expiry (#759),
  signing-key work (#749).
- **Boolean assertion coercion** wired into Measurement/Observation serializers
  (#881).
- Multiple migration graph conflicts resolved: merged migration heads at 0154,
  0176, 0177, 0178, 0179, 0184, 0185, 0200.

### Migration notes

| Item | Details |
|------|---------|
| Migration endpoint | `omop_core.0201_seed_hklabs_sccm` |
| Total migrations | 202 (up from 201 in v1.1.0) |
| Order requirement | Migrate through 0200, load Athena vocabularies, then apply 0201 |
| Vocabulary load time | ~11 seconds (`--concepts-only`) |
| Deployment gate | `start.sh` enforces Athena load before applying 0201 |
| Breaking changes | 1 — `seed_omop_concepts` removed, replaced by Athena vocabulary load |

---

## [1.1.0] — 2026-08-20

485 commits since 1.0.0. The defining change is architectural: `PatientRecord` became a
**derived read model**. Clinical data is written to OMOP CDM tables and the projection is
rebuilt from those facts; nothing writes the projection directly any more.

### Changed

- **`PatientRecord` is derive-only.** FHIR upload, CSV ingestion, and the OMOP CRUD
  endpoints all write OMOP rows and let the signal chain re-derive the projection. The
  FHIR write-through path was removed, and the derive-only contract is enforced and
  documented.
- **`PatientInfo` renamed to `PatientRecord`** across the backend. The legacy
  `/api/patient-info/` wire format (`patient_info`, `patient_info_id` keys) is unchanged.
- Profile writes moved from `PatientRecord` to `Person`.
- Password minimum raised to 12 characters, matching the analytics (PRism) app.

### Added

- **Bulk OMOP row writes** on the five clinical endpoints, accepting a JSON array with
  one transaction per batch, per-index validation errors, and a 1,000-row cap (#454).
  Idempotent by default: rows upsert on event identity so an ETL re-run or a retry
  converges instead of duplicating.
- **Deferred derivation** — `?skip_refresh=true` on bulk POST and row-level PATCH/DELETE,
  with `POST /api/v1/patient-records/{person_id}/refresh/` to derive afterwards (#532).
- **Patient portal**: first-class patient role, org-scoped invites, patient self-signup,
  consent grants, surveys, messages, allergies, immunizations, and settings (#264, #284).
- **Wearable ingestion** — Apple Watch and Garmin metrics normalized to OMOP rows, upload
  history, 30-day averages, and auto-detection of file type.
- **Vocabulary management** — releases, snapshots with release pinning and
  `X-Vocab-Release-Id` (#371), scope enforcement on release/snapshot endpoints (#344),
  concept graph endpoints (#232), and concept search by name and domain/class.
- **Derivation versioning and per-field OMOP provenance** (#358, #360).
- **PHR-S FM conformance controls** — persisted audit trail with review API (#295),
  retention/archival (#298), standards-based audit format (#303), tamper-evidence,
  hash chaining and break-glass access (#304, #318), authentication controls including
  lockout, no-reuse and forced change (#302, #319), message confidentiality (#308),
  entered-in-error and revision history (#307), FHIR exchange integrity and interchange
  agreements (#306), and terminology maintenance (#305).
- **Standard OMOP CDM 5.4 tables** for conformance, plus line-of-therapy unification with
  per-line outcome persistence in OMOP.
- **Follicular lymphoma pipeline** — Synthea FL generation with realistic timelines and
  mortality (#227), and FL → DLBCL transformation tracking.
- **mCODE FHIR import** support.
- `bulk_import_fhir_bundle` for fast synthetic cohort loading.
- CI now runs the pytest suite as well as the Django runner (#426).

### Fixed

- **Concept integrity**: clinical rows repointed off concepts that used their code as their
  id, `drug_exposure` repointed off locally-minted drug concepts, `Concept.source`
  backfilled, concept 0 metadata corrected, and locally-minted wearable concepts
  quarantined. Stopped minting fake HemOnc concepts (#236).
- CLL ALC unit mismatch in the iwCLL threshold (#544); MM disease-burden labs mapped to the
  LOINC codes real data uses (#537); direct bilirubin LOINC mapping; tumor and lymph-node
  size disambiguated.
- HER2 `Equivocal` receptor results preserved through the projection (#220);
  `valueString` persisted for lab observations (#218); `best_response` persisted (#205);
  `death_date` exposed through the patient_info view.
- Org cascade now deletes all person-FK tables it previously missed.
- 19 of 21 npm security advisories resolved.

---

## 2026-06-08

### Fixed
- **Migration 0085 idempotency — AddField** (#128)
  `sct_date` and `sct_eligibility` columns on `patient_info` already existed in production from a prior partial deploy. Wrapped both `AddField` operations in `SeparateDatabaseAndState` with `ADD COLUMN IF NOT EXISTS` SQL so the migration is safe to replay on any database state.

- **Migration 0085 idempotency — CreateModel** (#127)
  `vocabulary_sct_eligibility` table already existed in production. Wrapped `CreateModel` in `SeparateDatabaseAndState` with `CREATE TABLE IF NOT EXISTS` SQL, plus idempotent `DO $$ ... IF NOT EXISTS` blocks for unique constraints.

### Added
- **PHR-ETL integration — Person identity endpoints** (#124)
  Three new API endpoints for the phr-etl data pipeline:
  - `POST /api/persons/find_or_create/` — resolves `(actor_iss, actor_sub)` OIDC identity to a stable `person_id`; auto-provisions on first call; idempotent across organizations.
  - `PATCH /api/persons/{person_id}/` — fill-if-empty demographic patch; only writes fields that are `null` or a recognized placeholder; returns `updated_fields` list.
  - `GET /api/concepts/lookup/?lookup=VOCAB:code` — batch OMOP concept lookup by vocabulary + code pairs.
  - Added `actor_iss` / `actor_sub` fields to `Person` model with partial unique constraint.
  - 18 new backend tests.

---

## 2026-06-07

### Added
- **SCT fields for Multiple Myeloma** (#115)
  Stem cell transplant tracking on `PatientInfo`:
  - `stem_cell_transplant_history` (JSONField) — vocabulary: autologous SCT / allogeneic SCT / tandem SCT.
  - `sct_date` (DateField) — transplant date; future dates rejected by serializer.
  - `sct_eligibility` (JSONField) — new `SctEligibility` vocabulary (eligible/ineligible for autologous/allogeneic SCT); contradictory pairs rejected.
  - FHIR extensions in MM bundle generator + upload handler.
  - `populate_sct_sample_data` management command.
  - `audit_sct_history` management command.

---

## 2026-06-06

### Fixed
- **Dependabot vulnerabilities** (#122)
  Patched react-router open redirect and ws memory disclosure.

### Added
- **Patient surveys, race/MRD fields, MM FHIR bundle, LOT drug classification** (#118)
  Patient survey model and API; race field; MRD (minimal residual disease) field; updated MM FHIR bundle generator; line-of-therapy drug classification.

---

## 2026-06-04

### Fixed
- **Person ID removed from bulk_delete error responses** (#117) — resolves TODO #4.
- **Disease selection persists after save** (#116) — fixes issues #110 and #113.
- **Cross-org email fallback security fix + non-superuser sync auth tests** (#111) — resolves issues #17 and #18.

---

## 2026-06-01

### Fixed
- **OMOP sequence self-heal + ScopedTokenPermission test fix** (#108)
  Auto-repairs out-of-sync PostgreSQL sequences on startup; fixes test fallout from ScopedTokenPermission changes.
