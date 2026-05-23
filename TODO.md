# TODO

## Code-review: flagged issues (PR #72)

Issues identified during code review that require architectural decisions, profiling, or broader scope changes before fixing. Items marked ✅ were fixed in the review pass.

### Test coverage gaps for authorization edge cases
- **Severity:** medium / correctness
- `patient_portal/api/lab_results/tests.py`
- No tests for: actor_iss/actor_sub on-behalf-of flow, org-scoped sync rejection, pipe character validation in actor fields, concurrent PK generation, PATCH with invalid date, PersonalRepresentative `verification_status` enforcement, ProfessionalGroupAccess `expires_at` enforcement.
- **Action:** Add test cases for these authorization paths.

### Extract shared person auto-provisioning logic (DRY)
- **Severity:** medium / design
- `patient_portal/api/lab_results/sync.py:_resolve_person_from_identity` and `patient_portal/api/authentication.py:_ensure_person`
- Nearly identical logic in two places: check PatientUser, check email match, create Person with max(person_id)+1, create PatientUser. The sync copy previously had a bug the auth copy didn't (missing `transaction.atomic`), now fixed. Keeping two copies will cause drift again.
- **Action:** Extract into a shared `resolve_or_create_person(identity, email=None)` function in `patient_portal/services.py` and call from both places.

### _next_pk holds row locks for entire sync transaction
- **Severity:** medium / performance
- `patient_portal/api/lab_results/sync.py:49-56`
- Every `_next_pk` call acquires a row lock via `select_for_update()` held until the entire `@transaction.atomic` POST completes. 500 measurements = 500+ lock acquisitions serializing all concurrent syncs. Empty table race: if no rows exist, `select_for_update` locks nothing and two concurrent transactions can both create pk=1.
- **Action:** Migrate OMOP tables (Measurement, VisitOccurrence, CareSite, Concept) from manual IntegerField PKs to PostgreSQL sequences via Django's BigAutoField.

