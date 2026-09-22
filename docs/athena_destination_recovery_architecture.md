# Athena destination recovery

`python manage.py scrape_unmapped_destinations` reconciles code-mapping queue
proposals against authoritative Athena **Maps to** relationships. Single
destinations are approved; multiple destinations remain proposed for selection. It audits empty/legacy `none`/`proposed` queue statuses, including existing
proposals, and defaults to exact `ICD10` and `ICD10CM` vocabulary lookups.
Write eligibility is narrower than coverage auditing.

## Source order

For each eligible code, the command tries:

1. `~/Downloads/vocabulary_download_v5`, the local Athena TSV export.
2. If the local export cannot be read, the [configured Google Drive folder](https://drive.google.com/drive/u/1/folders/1HoRWGepqcH3pMKK03KNb1oWpaVs0Avl7).
   The existing Athena downloader selects one ZIP and streams its members.
3. The [Athena API](https://github.com/OHDSI/Athena), using exact vocabulary/code
   matches across all returned search pages, concept details and relationships.
4. The [Athena website](https://athena.ohdsi.org/search-terms/terms). An optional
   Playwright browser navigates the search and concept pages and reads the
   structured responses delivered to those pages, including the relationships
   table. It does not infer a destination from a search result title.

A readable local export replaces Drive for the entire run. Codes absent from
that export go directly to API/web lookup; they do not trigger a Drive download.
Only a missing or unreadable export causes a Drive lookup. The first API HTTP 403 disables that tier for the remainder of
the run, with one console notice. Its failed attempt is omitted from per-code
reports, and its zero-result summary counter is omitted. Successful API
discoveries made before the 403 retain their provenance. Other errors do not
disable the API. Local files and the Drive export are scanned once per batch, with
only selected concepts retained in memory. Drive is downloaded only when the local export is unavailable. No full export is extracted onto disk.

A source must match the exact vocabulary and code (ignoring code case and
surrounding whitespace). The source, outgoing relationship and destination must
be valid; the destination must be standard and in a supported clinical domain.
Only an unambiguous, single destination is approved. Ambiguous evidence stops
that code's cascade and is reported for review. A lower-priority source cannot
overrule conflicting evidence from an earlier source.

## Run and report

Preview the full cascade, without database writes:

```bash
python manage.py scrape_unmapped_destinations --dry-run --report /tmp/athena-preview.csv
```

Apply verified mappings and record where each was found:

```bash
python manage.py scrape_unmapped_destinations --report /tmp/athena-applied.csv
```

Useful options:

- `--vocabulary ICD10CM`: select an exact source vocabulary; repeat for others.
- `--path /path/to/export`: replace the default local directory.
- `--archive /path/to/export.zip`: use a local ZIP as the first source.
- `--gdrive URL`: set the fallback export location when local files are unavailable.
- `--lookup-vocabulary ICD10=ICD10CM`: explicitly interpret a source label
  using another Athena vocabulary. HT-One labels ICD-10-CM codes as `ICD10`
  (see `source_vocabularies.VOCAB_TO_UMLS_ROOT`); this option handles that
  documented convention without silently changing the stored source label.
  Both the stored and lookup vocabulary are recorded in the CSV.
- `--offline`: local source only, with no network requests.
- `--skip-download`: skip Drive, retaining local, API and web sources.
- `--no-web`: disable the optional browser source.
- `--limit 25`: process at most 25 eligible queue rows in ID order.
- `--delay 1 --timeout 30 --retries 2`: request pacing, timeout and bounded
  retries for API rate limits/server errors.
- `--skip-embeddings`: suppress the usual post-load embedding refresh.

`--report -` emits CSV on stdout and summaries on stderr. Otherwise the command
prints per-code outcomes unless a report file is supplied. The CSV includes
stored/lookup vocabulary, code, mapping row ID, source and target concept IDs, outcome, coverage,
reference, attempted sources, and errors. Its `tier` column uses exactly
`local directory`, `gdrive`, `API`, or `web`; it is blank when no source yielded
a mapping. A locally supplied ZIP is grouped with `local directory`, with its
actual path in `reference`. Per-vocabulary totals distinguish mappings found,
loaded (or `would_load` in a preview), unresolved, and skipped concurrent edits.
A mapping found but blocked by a local conflict is still counted as unresolved.
Coverage summaries count **unique vocabulary/code pairs**, including proposed
rows with existing candidate destinations. They separate `found`, `multiple_destinations`, `ambiguous`,
`not_found_any_method`, and `incomplete_lookup`. A definitive negative requires
a readable export and a completed web lookup, without an unresolved API error.
An API disabled after HTTP 403 is omitted, since the website is still checked.
Offline/disabled-web runs and failed lookups cannot establish absence across
all methods. Existing proposals can be counted as found while remaining
unchanged. Keep the CSV as the run receipt; do not commit environment-specific reports.

The browser fallback needs an additional operator dependency:

```bash
pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH=0
python scripts/install_athena_browser.py
```

An existing Chrome installation can be supplied with `--browser-executable`.
An operator-authorized Athena session can be provided using
`--browser-storage-state /private/path/state.json`. Keep that credential file
outside the repository. Login failures, unavailable dependencies and blocked
requests appear in the report; the command does not bypass access controls.

## Write safeguards

Only unreviewed, unlocked `proposed`/legacy `none`/empty-status queue rows with
no destination or an imported, non-curator-owned proposal qualify. Athena
replaces existing unreviewed HT-One/model proposals, including their destination
choices. Approved/rejected rows, curator provenance, reviewer stamps, updater
ownership and locks are preserved. Case-variant duplicates require review before
either row can be replaced. Rows are locked and checked against the
initial snapshot immediately before writing; all network work precedes locks.
Writes use bounded transactions of at most 100 queue rows.

Missing destination concepts are loaded under their actual Athena IDs.
Reference metadata must already be installed or present in an export; the
command never invents vocabulary, domain or concept-class identifiers. Existing
concepts are not overwritten. Identity/domain conflicts, invalid concepts or
locally deprecated vocabularies require a separate vocabulary refresh/review.
Each changed mapping retains a note recording the source, old proposed target
and replaced choices. Model-suggestion audit fields remain intact.

A single destination receives `status=approved`, `origin_system=athena` and
`source=Athena`, and appears in **ATHENA-MAPPED**. Multiple destinations receive
`status=proposed`, `origin_system=athena-multiple`, a null selected destination
and all Athena choices in the existing destination selector. HT-One choices are
replaced, not mixed into that list. These rows stay in **UNMAPPED** until a curator
selects and approves a destination. Approving one of the Athena choices moves
the row into **ATHENA-MAPPED** while preserving its alternatives.

The command changes mapping, destination concept and reference tables. It does
not rewrite existing patient facts. New imports use approved destinations through
the usual resolver. Repeated runs preserve approved mappings and existing Athena
choice sets. A failed batch rolls back that batch; earlier batches remain committed
and the CSV records the outcome.

## Offline deployment migration

Migration `0253_reconcile_athena_icd10_mappings` packages the reviewed evidence in
`omop_core/data/athena_recovery_20260921/mappings.json.gz`. The compressed snapshot
is checksum-verified before use and contains real Athena identifiers, reference
metadata and per-code provenance. It is ordinary Git content, not a machine-local
export or download prerequisite. The frozen loader uses historical models and
250-row transactions; it has no dependency on the evolving lookup command.

The artifact includes 11,174 source mappings (10,627 single destinations and
547 multiple destination sets), with 7,947 distinct standard target concepts.
It also identifies 57 existing target identities whose metadata was checked on
Athena's website. Only those external identities receive the packaged metadata
refresh, resolving stale domains and one retired flag. Local concept identities
and curator decisions remain protected. Refresh evidence is retained in the
artifact. No reference identifiers are synthesized.

The migration creates absent mapping rows and concepts, reconciles eligible
proposals and reports preserved/conflicting rows. Repeated execution does not
change completed mappings or their choices. Partial completion can be retried;
reverse migration deliberately retains this reference content. Existing patient
facts are not repointed. This is the same offline migration for HealthKey staging
and HealthTree upgrades.

## Staging verification

Staging means [Render staging](https://promop-staging.onrender.com). Before any
staging database operation, confirm `STAGING_DATABASE_URL` matches the active
`DATABASE_URL` configuration of both `promop-staging` and
`promop-staging-worker`, as described in [Render staging](render-staging-celery.md).
Run targeted tests and the complete packaged-data rehearsal on an isolated local
database. Preview the frozen loader on a read-only staging connection, inspect
its receipts, then apply **only through Django migrations**:

```bash
python manage.py migrate omop_core 0253_reconcile_athena_icd10_mappings
```

Configure that process with the verified staging database and deployment secret
key. Never point a test runner at staging. Retain migration logs and verify the
post-migration section counts and pending Athena choices.
