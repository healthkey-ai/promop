# ARTEMIS alignment to Episode bridge

`materialize_artemis_episodes` does not execute ARTEMIS. It is the only write
step after the pinned ARTEMIS runner has produced and a reviewer has accepted a
result artifact. It writes the existing OMOP Oncology `episode` and
`episode_event` tables through `upsert_therapy_line_episode`; it does not write
the PatientRecord projection directly.

## Why the alignment CSV is not an import file

ARTEMIS `processAlignments()` produces a relative-time alignment summary. Its
CSV includes fields such as `personID`, `component`, `t_start`, `t_end`, and
`CompleteDrugRecord`; it does **not** carry OMOP `drug_exposure_id` values or
calendar dates. It must never be handed directly to the materializer or used
to construct Episodes manually.

The runtime bridge retains a source-event ledger alongside ARTEMIS input. Each
ledger row is a local `DrugExposure` and includes its `drug_exposure_id`,
`person_id`, ancestor ingredient matching key, start date, and end date. It
reconstructs the same `CompleteDrugRecord` offsets used by ARTEMIS, then
matches every selected component by person, ingredient, and date to exactly
one local DrugExposure ID. If a component is missing, ambiguous, or incomplete
at that point, it fails closed and writes no adapter document. This is why the
adapter JSON is a runner artifact rather than a best-effort conversion of the
alignment CSV.

The runner retains `artemis-alignments.csv` for clinical audit and emits the
separate, adapter-ready `artemis-episodes.json` only after that resolution.

## Adapter document v1

```json
{
  "schema_version": "1",
  "episodes": [
    {
      "person_id": 123,
      "line_number": 1,
      "start_date": "2024-01-01",
      "end_date": "2024-03-01",
      "drug_exposure_ids": [9001, 9002],
      "regimen_concept_id": null,
      "outcome": null
    }
  ]
}
```

Only the two root keys and the fields shown above are accepted. `person_id`,
`line_number`, `start_date`, and a nonempty unique `drug_exposure_ids` list are
required. `end_date`, `regimen_concept_id`, and `outcome` are optional.

Before any row is written, the adapter validates the entire document and
checks that each Person, optional Concept, and DrugExposure exists; each
DrugExposure must belong to the stated person. It also requires `start_date`
to be the earliest selected exposure's start date and, if supplied, `end_date`
to be the latest selected exposure end date (or start date for open-ended
exposures). These checks make the source-event ledger independently verifiable
at the write boundary.

## Materialization

After the run artifact and its source-event resolution have clinical approval:

```bash
python manage.py materialize_artemis_episodes --input artemis-episodes.json --dry-run
python manage.py materialize_artemis_episodes --input artemis-episodes.json
```

Validation happens again in the non-dry run inside one transaction. A rerun
updates the same `(person_id, line_number)` Episode and replaces only its
canonical drug-exposure EpisodeEvents, so it converges instead of duplicating
events. An existing Episode whose `episode_source_value` is exactly `Manual`
is skipped and never changed by ARTEMIS.
