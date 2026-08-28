# ARTEMIS non-production execution runbook

This runbook defines the reproducible ARTEMIS runtime used to evaluate a
reviewed, scoped OMOP cohort. It does **not** author `Episode` or
`EpisodeEvent` rows. The subsequent PRomop adapter will consume the reviewed
alignment output and perform that write with its own audit and rollback policy.

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
after retaining the output manifest, alignment CSV, row counts, and approval
record. Secrets are injected at run time through the deployment secret store or
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

The expected artifacts are `artemis-alignments.csv` and
`artemis-run-manifest.json`. Do not import this CSV into PRomop manually. The
Episode/EpisodeEvent adapter is a separate, reviewed operation.

## Validation and audit

Before accepting results, retain the image digest, upstream commit, cohort JSON
digest, config validation transcript, database role grants, cohort row count,
the manifest, alignment CSV, runner logs, and reviewer approvals in the change
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
