# Live Athena destination-domain reconciliation

`reconcile_athena_domains` checks the official current Athena domain of standard
destination concepts referenced by one stored source-code vocabulary. Athena is
the authority for the concept domain. This command updates only
`concept.domain_id`; it does not replace destinations or alter names, concept
classes, standard flags, validity, mapping metadata, curator decisions or patient
facts.

## Scope and lookup

The required argument is an exact `SourceCodeConceptMapping.source_vocabulary_id`,
for example `ICD10` or `ICD10CM`. It selects both current target concepts and
`MappingDestinationCandidate` choices across all statuses. Each distinct local
standard destination is checked once, regardless of how many source codes point
to it. Other source vocabularies are outside the selection. A concept is shared
vocabulary metadata, so correcting it benefits every reference to that concept.

The command visits the Athena concept-detail page by OMOP concept ID and reads
the detail response delivered by the website. It uses the existing browser
transport, with no local vocabulary export, bulk download, direct API fallback,
or bundled data snapshot. Browser sessions close before synchronous database
operations. All lookups are fresh on each run.

Corrections require matching concept ID, vocabulary and code, an external local
concept, and a current standard Athena concept with a known local Domain entry.
Locally authored concepts and reserved local IDs are protected. Missing evidence,
identity differences, a no-longer-standard/active Athena concept, and missing
Domain reference rows are reported without inventing a correction. No lookup
failure is reported as a successful match.

## Running it

Render and Docker builds install Playwright and Chromium and verify a headless
browser launch. No installation is needed in a deployed shell. For local setup:

```sh
pip install -r requirements.txt
export PLAYWRIGHT_BROWSERS_PATH=0
python scripts/install_athena_browser.py
```

The Render Blueprint sets `PLAYWRIGHT_BROWSERS_PATH=0` for all four active
Python web/worker services and runs `python scripts/install_athena_browser.py`
after dependency installation. Chromium is retained inside the Python package,
so changing the build/runtime home directory does not lose it. The helper verifies
a real browser launch and rendering before the build succeeds. It installs the
headless Chromium shell used by both Athena commands.

Docker images use the same helper with `--with-deps`; the GCP image installs OS
libraries in its final runtime stage. The Python CI job also exercises the real
Linux browser installation and launch. The native Render build relies on its
preinstalled Linux libraries; the launch check reports an incompatible image at
build time rather than during a curator job.

Existing dashboard-managed Render services must synchronize the Blueprint's
build command and `PLAYWRIGHT_BROWSERS_PATH` setting. A repository change alone
does not replace a dashboard override. Production build commands conditionally
run the helper when the deployed `main` revision contains it, preserving older
production revisions.

Audit only (the default):

```sh
python manage.py reconcile_athena_domains ICD10 --report /tmp/icd10-domains.csv
```

Apply verified domain corrections:

```sh
python manage.py reconcile_athena_domains ICD10 --apply --report /tmp/icd10-domains-applied.csv
```

Repeat separately for `ICD10CM` when both stored source vocabularies should be
checked. No implicit alias expands the requested scope. `--database` selects a
configured Django database alias; running on the HealthTree deployment checks
that instance's own destinations. No direct operator access from HealthKey is
needed.

`--delay` defaults to one second between navigations. `--timeout` defaults to 30
seconds per response, and `--batch-size` defaults to 25 (maximum 100).
`--browser-executable` and `--browser-storage-state` support the same optional
browser configuration as destination recovery. An unknown source vocabulary
fails clearly. A known vocabulary with no standard destinations reports zero.

## Writes, reports and reruns

Each correction locks and rereads the concept after the web lookup. Concurrent
changes to its identity, provenance, standard status or domain are preserved and
reported for rechecking. A concurrent change that already set the Athena domain
is a no-op. The SQL update names only `domain_id`.

A CSV receipt includes the source vocabulary, concept ID/vocabulary/code, local
and Athena domains, outcome, reason, Athena page URL and check timestamp.
`--report -` writes CSV to stdout and progress to stderr. Without `--report`,
results and counts are printed. Partial lookup failures produce a nonzero exit
status after the report is written; successful corrections in an apply run stay
committed and reruns safely check them again.

## Relationship to the staging conflict workbook

The 22 September workbook's 77 source-code conflicts compared destination
metadata for the same concept IDs: 55 domain differences and one standard/
validity discrepancy across 56 distinct concepts. It was a pre-reconciliation
snapshot. PR #1543's migration 0253 handles its separately verified recovery
payload. This command is not restricted to that payload or the workbook; it
checks whichever standard destinations exist on the selected instance.

This change deliberately adds no reference-data archive and no network-dependent
startup migration. It is an explicit management command against the live website.
