# Lab Results Deduplication Architecture

PROMOP lab-result sync is idempotent for repeated uploads from hk-labs. Re-uploading the
same report creates a new visit/commit record but reuses matching measurements instead of
duplicating clinical facts.

## Write Path

`patient_portal/api/lab_results/sync.py` checks for an existing `Measurement` before
creating a new one. A measurement matches when the patient, measurement date, concept or
source concept, numeric value, and string value are equal using null-safe comparison.

If a match exists, sync reuses the existing `measurement_id`. If no match exists, sync
allocates a new primary key and creates a `Measurement`.

Every sync still creates a new `VisitOccurrence`, representing the upload/commit event.

## Ownership Model

`MeasurementOwnership` links each upload visit to each measurement contributed by that
upload:

```text
MeasurementOwnership(measurement_id, visit_occurrence_id)
```

The table has a uniqueness constraint over the pair and an index on
`visit_occurrence_id`. The original `Measurement.visit_occurrence_id` remains pointed at
the creating visit for OMOP compatibility; ownership rows track all uploads that reference
the measurement.

## Sync Response

The sync response returns all measurement ids, including reused ones, plus created and
deduplicated counts:

```json
{
  "visit_occurrence_id": 12,
  "measurement_ids": [101, 102, 103],
  "count": 67,
  "created_count": 0,
  "deduplicated_count": 67
}
```

hk-labs can treat deduplicated syncs as successfully saved.

## Delete Path

`VisitDeleteView` deletes ownership rows for the visit being removed, then deletes only
measurements with no remaining owners. Measurements shared by another upload remain in
PROMOP until the last owning upload is deleted.

## Backfill

Migration `0079_measurement_ownership.py` creates `MeasurementOwnership` and backfills an
ownership row for every existing measurement that already has a visit id.

## Edge Cases

| Scenario | Behavior |
|---|---|
| Same PDF uploaded twice | Second upload reuses measurements and creates a new visit |
| Same test/date/value from different uploads | Measurement is shared through ownership rows |
| Same test/date with a different value | New measurement is created |
| Qualitative values with null numeric value | Null-safe comparison preserves dedup behavior |
| Delete one duplicate upload | Shared measurements remain |
| Delete the last upload | Orphaned measurements are deleted |
