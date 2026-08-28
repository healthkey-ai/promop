# ARTEMIS non-production execution runbook

This runbook defines the reproducible ARTEMIS runtime used to evaluate a
reviewed, scoped OMOP cohort. It does **not** author `Episode` or
`EpisodeEvent` rows. It produces the validated JSON artifact consumed by the
separate PRomop Episode adapter, which performs that write with its own audit
and rollback policy.

ARTEMIS is pinned to upstream commit `242b5a24864b85a44c62d95a98cbaa2d16c55539`
(package version 1.6.0). The image pins R 4.4.3 and records both the ARTEMIS
revision and a digest of the cohort definition in every run manifest. Updating
any pin requires a reviewed PR, a non-production acceptance run, and an update
to this document.

## Boundaries and permissions

ARTEMIS reads `person`, `drug_exposure`, and vocabulary data in the supplied
OMOP CDM schema. Its cohort generation creates temporary cohort artifacts in a
separate write schema. Therefore the database role must have:

| Scope | Required privilege | Forbidden privilege |
| --- | --- | --- |
| CDM schema | `USAGE`, `SELECT` only | `INSERT`, `UPDATE`, `DELETE`, DDL |
| ARTEMIS write schema | `USAGE`, `CREATE`, and DML only | access to another organisation's schema |
| PRomop `episode` / `episode_event` | none for this runner | any write privilege |

Use one disposable, run-specific write schema such as
`artemis_791_uat_20260828`; it must not equal the CDM schema. Delete it only
after retaining the output manifest, alignment CSV, adapter JSON, row counts,
and approval record. Secrets are injected at run time through the deployment secret store or
an untracked `--env-file`; they are never committed, placed in cohort JSON, or
passed in a command line.

## Inputs

The cohort is a reviewed OHDSI cohort-definition JSON. Keep it in a protected
input directory, mount it read-only, and give it a meaningful versioned name.
Limit its expression to the organisation/person scope and the intended disease
population. Record the reviewer, cohort definition commit/hash, expected person
count, intended condition/regimen set, and run owner in the change ticket.

The container expects these environment variables:

| Variable | Meaning |
| --- | --- |
| `ARTEMIS_DBMS` | `postgresql` |
| `ARTEMIS_DB_SERVER`, `ARTEMIS_DB_PORT` | Database host/database and port; no credentials in this value |
| `ARTEMIS_DB_USER`, `ARTEMIS_DB_PASSWORD` | Ephemeral least-privilege runner credentials |
| `ARTEMIS_CDM_SCHEMA` | Read-only PRomop CDM schema |
| `ARTEMIS_WRITE_SCHEMA` | New disposable ARTEMIS-only schema |
| `ARTEMIS_COHORT_JSON` | In-container read-only path, normally `/work/input/cohort.json` |
| `ARTEMIS_COHORT_NAME` | Traceable cohort-table prefix |
| `ARTEMIS_CONDITION` | Regimen set; defaults to `all` |

## Build and dry run

Build on an approved CI/runner, then record the resulting image digest:

```bash
docker build -f ops/artemis/Dockerfile -t promop-artemis:791 .
docker image inspect promop-artemis:791 --format '{{index .RepoDigests 0}}'
docker run --rm --entrypoint Rscript promop-artemis:791 --version
```

Create an untracked `artemis.env` from the organisation's secret manager. First
perform a structural check; it opens no database connection:

```bash
docker run --rm --env-file artemis.env \
  -v "$PWD/cohorts:/work/input:ro" --entrypoint validate-artemis-config \
  promop-artemis:791
```

Then perform the default runner dry run. It validates required configuration and
writes `artemis-run-manifest.json`; it does **not** open a database connection,
create cohort tables, or execute alignment.

```bash
docker run --rm --env-file artemis.env \
  -v "$PWD/cohorts:/work/input:ro" -v "$PWD/artemis-output:/work/output" \
  promop-artemis:791
```

## Non-production acceptance execution

Production execution is separately authorised and is not enabled by this
runbook. Before a UAT run, the clinical owner and data owner approve the exact
cohort hash, environment, image digest, regimen set, expected cohort size, and
the dedicated write schema. Confirm that the role has no DML or DDL privilege
on the CDM schema and no access to production.

Only after that approval, add both gates to the secret environment and run in a
non-production database:

```bash
ARTEMIS_MODE=execute
ARTEMIS_NONPROD_APPROVED=yes
ARTEMIS_ALLOW_WRITE=yes
```

```bash
docker run --rm --env-file artemis.env \
  -v "$PWD/cohorts:/work/input:ro" -v "$PWD/artemis-output:/work/output" \
  promop-artemis:791
```

The expected artifacts are `artemis-alignments.csv` (audit-only),
`artemis-episodes.json`, and `artemis-run-manifest.json`. The JSON has the
strict adapter contract:

```json
{
  "schema_version": "1",
  "episodes": [{
    "person_id": 123,
    "line_number": 1,
    "start_date": "2024-01-01",
    "end_date": "2024-03-01",
    "drug_exposure_ids": [9001, 9002]
  }]
}
```

`person_id`, `line_number`, `start_date`, and non-empty local
`drug_exposure_ids` are required; `end_date`, `regimen_concept_id`, and
`outcome` are optional. The runner derives local exposure IDs using a read-only
query over the *same scoped cohort*. It reconstructs ARTEMIS's encoded drug
record positions and requires each aligned ingredient/date to resolve to
exactly one local `drug_exposure_id`. It aborts rather than emitting JSON for
an ambiguous or missing link; it never guesses by using every exposure in a
date range. ARTEMIS regimen labels do not reliably identify a local OMOP
concept, so this initial bridge deliberately omits `regimen_concept_id` rather
than minting or guessing a concept. It preserves the raw alignment CSV for
clinical review.

Use a fresh, empty output directory for each execution. The runner refuses to
overwrite an existing `artemis-episodes.json`, preventing a failed run from
leaving a stale artifact that could later be materialized by mistake.

After clinical review, validate before writing:

```bash
python manage.py materialize_artemis_episodes \
  --input artemis-output/artemis-episodes.json --dry-run
```

Only the separately approved materializer can write `Episode` and
`EpisodeEvent` rows; never import the CSV directly.

## Validation and audit

Before accepting results, retain the image digest, upstream commit, cohort JSON
digest, config validation transcript, database role grants, cohort row count,
the manifest, alignment CSV, adapter JSON, runner logs, and reviewer approvals in the change
record. Validate that:

1. the cohort count is the approved scoped count;
2. every aligned person belongs to that cohort and organisation;
3. all output dates are valid clinical dates and each regimen is traceable to
   its source `drug_exposure` records;
4. sampled results are clinically reviewed, including no-regimen exposures and
   overlapping/ambiguous alignments; and
5. no object outside the disposable write schema changed.

Failure of any check rejects the output; do not send it to the Episode adapter.

## Rollback and incident response

ARTEMIS writes only its disposable write schema. Stop the container, preserve
logs and the manifest, revoke the ephemeral credential, and have the database
owner drop the exact named write schema after evidence retention. Do **not**
drop a schema through a wildcard or use the CDM schema as a rollback target.
Because this runner has no Episode/EpisodeEvent privilege, no patient-facing
therapy data requires rollback at this stage. If the later adapter is run, use
its recorded run identifier and rollback procedure instead.
