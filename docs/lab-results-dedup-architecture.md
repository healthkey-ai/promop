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

## Concept Resolution

hk-labs sends `match_method` on every measurement — which of its own tiers resolved the
test (`loinc`, `alias_exact`, `name_fallback`, `manual`, `unmatched`) — plus
`test_name_normalized` and, where the report carried one, a lab-native
`source_code`/`source_code_system`.

A test whose LOINC code resolves is answered by that code and needs no curation. Every
other test goes through `omop_core/services/code_mapping.resolve_source_code`, which
honours an approved mapping, otherwise mints under `HK-Labs` **and** files a *proposed*
`SourceCodeConceptMapping` with `origin='import'`. Sync used to mint inline instead, so
hk-labs-originated unresolved tests never reached the Code Mapping review queue
(hk-labs#50).

- **What keys the proposal** — the lab-native code when there is one, else the normalized
  test name. The measurement stores the printed *name* in `measurement_source_value`, so
  the proposal's description carries that name; `_source_value_match` matches stored rows
  on the code or the description, which is how approving a code-keyed mapping still
  re-points rows the code never appeared on.
- **Where `match_method` goes** — the proposal's `origin_system`, as `hk-labs:<method>`.
  A curator reading the queue sees which tier produced the row without opening the report.
- **Destination column** — a resolved or minted concept is written to
  `measurement_concept_id` (and, when minted, also to `measurement_source_concept_id`).
  `repoint_clinical_rows` rewrites the destination column on approval, so a mint parked
  only in the source column would strand these rows at the invented concept.
- **A LOINC code with no concept loaded here** resolves to nothing and is recorded as a
  gap, never minted — an HK concept shadowing a real LOINC one is what
  `remap_shadow_concepts` exists to undo. Such rows sit at concept 0, and dedup falls back
  to matching on `measurement_source_value` so two unresolved tests from one draw are not
  collapsed into one.

Resolution is per distinct code and cached for the request, so concept work stays flat in
the number of measurements and `occurrence_count` counts syncs, not rows.

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
