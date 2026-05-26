# Lab Results Deduplication Plan

## Problem

When the same lab report is uploaded multiple times (e.g., same PDF re-uploaded, or two versions of the same report), hk-labs commits each upload independently. Each commit calls ctomop's `POST /api/lab-results/sync/`, creating duplicate measurements for the same patient/date/test/value.

**Requirements:**
- Commit/sync must never fail — always return success to hk-labs
- Duplicate measurements must not be created in ctomop
- Uploads in hk-labs should show as "saved/completed" regardless of dedup
- Measurements are only deleted from ctomop when ALL uploads referencing them are deleted

## Solution: Idempotent Sync with Ownership Tracking

### 1. Dedup on Write (ctomop sync endpoint)

In `SyncView.post()`, before creating each measurement, check for an existing match:

```sql
SELECT measurement_id FROM measurement
WHERE person_id = %s
  AND measurement_date = %s
  AND (measurement_concept_id = %s OR measurement_source_value = %s)
  AND value_as_number = %s  -- NULL-safe comparison for qualitative results
```

Match criteria: `(person_id, measurement_date, concept_id OR source_value, value_as_number)`

- If match found → reuse existing measurement_id, skip creation
- If no match → create new measurement as before

A new `VisitOccurrence` is always created (represents the upload/commit event).

### 2. New Model: MeasurementOwnership

```python
class MeasurementOwnership(models.Model):
    measurement_id = models.IntegerField()
    visit_occurrence_id = models.IntegerField()

    class Meta:
        db_table = "measurement_ownership"
        unique_together = [("measurement_id", "visit_occurrence_id")]
        indexes = [
            models.Index(fields=["visit_occurrence_id"]),
        ]
```

**On sync:**
- For every measurement (created or deduplicated), insert an ownership record linking it to the new visit
- The Measurement's `visit_occurrence_id` FK stays pointing to the original creating visit (OMOP-compliant)

### 3. Updated Sync Response

```json
{
  "visit_occurrence_id": 12,
  "measurement_ids": [101, 102, 103],
  "count": 67,
  "created_count": 0,
  "deduplicated_count": 67
}
```

hk-labs doesn't need to distinguish — it stores `visit_occurrence_id` and considers the upload saved.

### 4. Updated Delete Logic (VisitDeleteView)

When deleting a visit:

1. Remove all `MeasurementOwnership` rows for that `visit_occurrence_id`
2. Find measurements that now have zero ownership records remaining
3. Delete only those orphaned measurements
4. Delete the `VisitOccurrence` itself

```python
# Pseudocode
ownership_measurement_ids = MeasurementOwnership.objects.filter(
    visit_occurrence_id=visit_id
).values_list("measurement_id", flat=True)

MeasurementOwnership.objects.filter(visit_occurrence_id=visit_id).delete()

orphaned = [
    m_id for m_id in ownership_measurement_ids
    if not MeasurementOwnership.objects.filter(measurement_id=m_id).exists()
]

Measurement.objects.filter(measurement_id__in=orphaned).delete()
VisitOccurrence.objects.filter(visit_occurrence_id=visit_id).delete()
```

### 5. hk-labs Changes

None required. The existing flow works:
- `ctomop_client.sync_measurements()` — returns success with measurement_ids (created or deduplicated)
- `ctomop_client.delete_visit()` — ctomop handles ownership check internally

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| Same PDF uploaded twice | Second commit deduplicates all 67 measurements, creates new visit, adds ownership records |
| Same test on same day from different labs | Different `care_site` but dedup matches on value — acceptable for patient-uploaded reports |
| Same test, same day, different value | Not deduplicated (different `value_as_number`) — both kept |
| Qualitative result (value=NULL, value_string="Negative") | Match on `value_as_number IS NULL AND measurement_source_value` |
| Delete one of two duplicate uploads | Ownership records removed, measurements preserved (still owned by other visit) |
| Delete last remaining upload | Ownership count drops to 0, measurements deleted |

## Migration Path

1. Create `MeasurementOwnership` table
2. Backfill: for every existing `Measurement`, create an ownership record with its current `visit_occurrence_id`
3. Update `SyncView.post()` with dedup logic
4. Update `VisitDeleteView` with ownership-aware delete
5. Deploy — no hk-labs changes needed

## Files to Modify

- `omop_core/models.py` — add `MeasurementOwnership` model
- `patient_portal/api/lab_results/sync.py` — add dedup check in `_build_measurement` loop
- `patient_portal/api/lab_results/views.py` — update `VisitDeleteView` delete logic
- New migration for `measurement_ownership` table + backfill
