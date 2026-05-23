# TODO

## Code-review: flagged issues (PR #72)

### _next_pk holds row locks for entire sync transaction
- **Severity:** medium / performance
- `patient_portal/api/lab_results/sync.py:49-56`
- Every `_next_pk` call acquires a row lock via `select_for_update()` held until the entire `@transaction.atomic` POST completes. 500 measurements = 500+ lock acquisitions serializing all concurrent syncs. Empty table race: if no rows exist, `select_for_update` locks nothing and two concurrent transactions can both create pk=1.
- **Action:** Migrate OMOP tables (Measurement, VisitOccurrence, CareSite, Concept) from manual IntegerField PKs to PostgreSQL sequences via Django's BigAutoField.

