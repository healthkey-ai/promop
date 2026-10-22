# Episode-backed treatment fields and OHDSI ARTEMIS plan

## Goal

Make every `PatientRecord` treatment-line field (`first_line_*`,
`second_line_*`, and `later_*`) a code-computed projection of the patient's
OMOP `episode` and `episode_event` records.  Use OHDSI ARTEMIS to materialize
those records from OMOP CDM drug exposures; do not use a formula or make the
flat PatientRecord fields independently editable.

## Current state — 2026-08-28

- [x] The OMOP Oncology Extension `episode` and `episode_event` tables and
  Django models already exist (`omop_oncology`). No new tables are needed.
- [x] `episode_service.upsert_therapy_line_episode` writes a Treatment Regimen
  Episode and links `drug_exposure` rows using field concept `1147094`.
- [x] `patient_record_service` can project existing Episodes and EpisodeEvents.
- [ ] Refresh currently falls back to PROMOP's ARTEMIS-lite inference when no
  Episodes exist. This makes PatientRecord appear authoritative and must end.
- [ ] ARTEMIS is not installed or run as a reproducible PROMOP job. It is an R
  package which requires a CDM connection, a cohort definition, regimen and
  valid-drug inputs, and a writable work schema.

## Delivery checklist

### 1. PatientRecord projection contract — issue #789

- [ ] Classify all treatment-line fields as **Computed (Episode/EpisodeEvent)**
  in descriptors and UI explanations.
- [ ] Project only persisted Episode/EpisodeEvent links; no dry-run LOT
  inference from a PatientRecord refresh.
- [ ] Preserve the existing episode projection semantics: Episode number maps
  to first/second/later lines; EpisodeEvent field concept `1147094` links
  DrugExposure; the Episode's source/object concept is the asserted regimen.
- [ ] Clear stale line fields when the persisted Episode set changes or is empty.
- [ ] Add integration tests for multiple lines, dangling/non-drug events, and
  refreshes with DrugExposure rows but no Episode.

### 2. ARTEMIS adapter and materializer — issue #790

- [ ] Vendor neither ARTEMIS output nor patient data. Invoke a pinned ARTEMIS
  R environment through a management-command adapter with explicit connection
  and work-schema configuration.
- [ ] Require a cohort input and explicit `--person-id` / `--cohort` scope;
  fail closed when R, ARTEMIS, configuration, or output validation is absent.
- [ ] Convert validated ARTEMIS alignment output into `Episode` and
  `EpisodeEvent` rows through `upsert_therapy_line_episode`; retain links to
  the exact backing DrugExposure ids and tag the source as `ARTEMIS`.
- [ ] Make reruns idempotent and replace only ARTEMIS-owned line membership;
  never overwrite manually authored Episodes without an explicit force option.
- [ ] Add a fixture-driven adapter contract test; R itself is exercised in a
  separate integration environment, not unit-test CI.

### 3. Runtime, execution, and acceptance — issue #791

- [ ] Define a container/image or documented runner containing R, ARTEMIS,
  `DatabaseConnector`, `CDMConnector`, and the PostgreSQL driver, pinned to
  reviewed versions.
- [ ] Add a runbook for cohort creation, secrets, CDM/write schemas, ARTEMIS
  inputs, dry-run, execution, validation, rollback, and audit output.
- [ ] Run ARTEMIS against an approved non-production patient cohort first;
  validate Episode/EpisodeEvent counts, drug-link completeness, and resulting
  PatientRecord projections before authorizing production execution.
- [ ] Production execution is a separate operational change requiring the
  cohort, credentials, and explicit authorization; it is not performed by a
  code deploy.

## Architecture

```
DrugExposure rows ── ARTEMIS alignment ──> Episode + EpisodeEvent
                                                │
                                                └── refresh_patient_record
                                                        │
                                                        └── computed treatment fields
```

ARTEMIS does not write PROMOP's Django tables directly. The adapter validates
its output and uses the existing canonical episode writer, preserving OMOP
concepts, provenance and idempotency.

## Verification gates

1. Unit/integration: projection reads only persisted Episodes and linked
   DrugExposures; raw exposures alone do not populate therapy lines.
2. Adapter contract: a known ARTEMIS output creates the expected Episodes and
   EpisodeEvents and is idempotent on rerun.
3. Acceptance: every Episode has valid regimen/type concepts and every linked
   `event_id` resolves to a DrugExposure for that patient.
4. Operational: a dry run emits counts and validation errors without writes;
   a scoped run records an audit report and can be rerun safely.

## Progress log

Update this checklist and add the merged PR next to each issue as work lands.
