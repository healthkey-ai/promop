# TODO

## Backend-review: flagged issues (PR #72)

These were identified during the backend review of the `feat/hk-labs-integration` branch but require architectural decisions or profiling before fixing.

### ServiceTokenAuthentication returns arbitrary superuser
- `patient_portal/api/authentication.py:134`
- `Identity.objects.filter(is_superuser=True).first()` picks a random superuser. A compromised service token gets full database access with no per-patient scoping. Audit logs attribute writes to whichever admin happens to be `.first()`.
- **Action:** Create a dedicated non-superuser service Identity (e.g. `issuer='urn:service', sub='hk-labs-service'`) with only the permissions service-to-service calls need.

### _build_cards loads all measurements into memory
- `patient_portal/api/lab_results/views.py` — `ResultsSummaryView._build_cards`
- Fetches ALL measurements for a person, groups in Python, then discards most (MAX_VALUES_PER_CONCEPT=10). A patient with thousands of measurements loads them all before pagination.
- **Action:** Paginate at the concept level first (query distinct concept IDs), then fetch only measurements for concepts on the current page.

### select_for_update holds row locks for entire sync transaction
- `patient_portal/api/lab_results/sync.py:50-56`
- Every `_next_pk` call acquires a row lock held until the entire `@transaction.atomic` POST completes. 500 measurements = significant lock contention under concurrent load.
- **Action:** Consider PostgreSQL sequences for PK generation instead of the `max(pk) + 1` pattern.

### Identity.sub unique=True conflicts with multi-issuer OIDC
- `patient_portal/models.py:50`
- `sub` is the Django `USERNAME_FIELD` (requires `unique=True`), but OIDC `sub` is only unique within an issuer. Two issuers with the same `sub` value will collide. Not a problem with a single Firebase provider, but blocks adding a second OIDC provider.
- **Action:** Introduce a synthetic unique field (e.g. `uid = f"{issuer}:{sub}"`) as `USERNAME_FIELD`, or switch to email-based lookup.

### Test coverage gaps for authorization edge cases
- `patient_portal/api/lab_results/tests.py`
- No tests for: actor_iss/actor_sub on-behalf-of flow, org-scoped sync rejection, pipe character validation in actor fields, concurrent PK generation, PATCH with invalid date.
- **Action:** Add test cases for these authorization paths.
