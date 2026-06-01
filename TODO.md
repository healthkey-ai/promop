# TODO

## Backend review findings (2026-05-23)

### Critical

#### #1 ServiceTokenAuthentication falls back to arbitrary superuser
- **Severity:** critical / security
- `patient_portal/api/authentication.py:108-115`
- When the dedicated service identity (`urn:service|hk-labs-sync`) doesn't exist, auth falls back to `Identity.objects.filter(is_superuser=True).first()`. Silently impersonates a real admin, bypasses all authorization, corrupts provenance audit trails — HIPAA accountability gap.
- **Action:** Remove the superuser fallback. Fail closed (return None).

#### #2 _resolve_person_id allows org-scoped tokens to bypass access check
- **Severity:** critical / security
- `patient_portal/api/lab_results/views.py:70-83`
- When `can_access_patient()` fails but `get_request_org()` returns non-None, the function returns `(pid, None)` granting access. The org-person membership check happens later in each view but the logic is inverted.
- **Action:** Move the org-person membership check into `_resolve_person_id` itself.

#### #3 No rate limiting on auth or write endpoints
- **Severity:** critical / security
- `ctomop/settings.py`
- No `DEFAULT_THROTTLE_CLASSES` or per-view throttling. SERVICE_AUTH_TOKEN can be brute-forced. Sync endpoint accepts 500 measurements/request with no rate limit.
- **Action:** Add `DEFAULT_THROTTLE_CLASSES` and `DEFAULT_THROTTLE_RATES` to REST_FRAMEWORK settings. Add stricter per-view throttles on sync/auth endpoints.

### High

#### #6 EXCLUSIVE table locks per measurement in sync loop
- **Severity:** high / performance
- `patient_portal/api/lab_results/sync.py:49-60,428`
- `_next_pk()` acquires EXCLUSIVE lock on the entire measurement table, called once per measurement (up to 500). Fully serializes all concurrent sync requests.
- **Action:** Use PostgreSQL sequences (`nextval()`) instead of `LOCK TABLE + MAX(pk)`.

#### #7 Sequential INSERT per measurement instead of bulk_create
- **Severity:** high / performance
- `patient_portal/api/lab_results/sync.py:284-293`
- 500 individual `Measurement.objects.create()` calls = 500 DB round trips per sync request.
- **Action:** Pre-allocate IDs via sequence, build objects in a list, use `bulk_create()`.

#### #11 MeasurementDetailView.patch is not atomic
- **Severity:** high / HIPAA
- `patient_portal/api/lab_results/views.py:570-581`
- `m.save()` commits the measurement update, then `ProvenanceRecord.objects.create()` creates the audit record. If provenance fails, the measurement is modified without an audit trail.
- **Action:** Wrap in `transaction.atomic()`.

#### #12 VisitDeleteView.delete is not atomic
- **Severity:** high / HIPAA
- `patient_portal/api/lab_results/views.py:659-676`
- Provenance created, then measurements deleted, then visit deleted — not in a transaction. Partial failures leave inconsistent state.
- **Action:** Wrap in `transaction.atomic()`.

### Medium

#### #4 Person ID leaked in error response
- **Severity:** medium / security
- `patient_portal/api/lab_results/sync.py:211`
- `f'Person {person_id} does not exist.'` enables person_id enumeration.
- **Action:** Replace with generic `'Person not found.'`.

#### ~~#5 ScopedTokenPermission bypasses scope enforcement for partner auth~~ ✓ FIXED
- service-token → full access; staff/superuser → full access; patients → safe methods + PATCH only (POST/DELETE denied).

#### #8 _get_or_create_hk_concept runs per-measurement without caching
- **Severity:** medium / orm
- `patient_portal/api/lab_results/sync.py:448-471`
- For LOINC-unmatched tests, each measurement does a DB query. Race condition between concurrent requests can create duplicate concept_codes.
- **Action:** Pre-build an `hk_concept_cache` before the loop. Add unique constraint on `(vocabulary_id, concept_code)`.

#### #9 Missing db_index on authorization lookup columns
- **Severity:** medium / database
- `omop_core/models.py:107,183`
- `PatientGroupMembership.person_id` and `PersonalRepresentative.person_id` lack standalone indexes but are filtered in `can_access_patient()` on every request.
- **Action:** Add `db_index=True` to both fields.

#### #10 resolve_or_create_person race condition on concurrent first-login
- **Severity:** medium / database
- `patient_portal/services.py:37-49`
- Two concurrent requests for a brand-new identity can both enter Person creation. Second `PatientUser.objects.create()` fails with IntegrityError (OneToOneField).
- **Action:** Catch `IntegrityError` on `PatientUser.create` and retry lookup.

#### #13 MeasurementDetailView.patch uses request.data without serializer
- **Severity:** medium / drf
- `patient_portal/api/lab_results/views.py:502-582`
- Reads fields directly from `request.data` bypassing DRF validation.
- **Action:** Create a `MeasurementUpdateSerializer` and use `serializer.is_valid(raise_exception=True)`.

#### #14 _hydrate_page fetches ALL measurements then truncates in Python
- **Severity:** medium / orm
- `patient_portal/api/lab_results/views.py:235-270`
- Queries all matching measurements with no LIMIT, discards all but 10 per concept in Python.
- **Action:** Use a window function (`ROW_NUMBER() OVER (PARTITION BY concept_id)`) to limit at the DB level.

#### #15 _ensure_concept returns None without clear error propagation
- **Severity:** medium / python
- `patient_portal/api/lab_results/sync.py:98-126,254-255`
- If `_ensure_concept()` returns None for required concepts, the FK assignment causes an `IntegrityError` with a confusing traceback.
- **Action:** Add explicit null checks and return 503 with a clear message.

#### ~~#17 Email fallback in _resolve_person_id can match wrong patient~~ ✓ FIXED
- Email fallback now disabled for non-superuser users without org scope; org-filtered when org present; superusers retain cross-org access.

#### #18 SyncViewTest uses superuser, masking authorization bugs
- **Severity:** medium / testing
- `patient_portal/api/lab_results/tests.py:96-99`
- All core sync tests use a superuser, so `can_access_patient()` always returns True. Authorization path never exercised.
- **Action:** Add sync tests with a non-superuser identity that has self-access (PatientUser link).

### Low

#### #16 Provider registry module-level cache without invalidation
- **Severity:** low / python
- `patient_portal/api/providers/registry.py:9-30`
- `_providers` is set once and never cleared. Makes testing difficult.
- **Action:** Add a `clear_providers()` function for tests.

---

## Previous findings

### _next_pk holds row locks for entire sync transaction (superseded by #6/#7 above)
- `patient_portal/api/lab_results/sync.py:49-56`
- Superseded by findings #6 and #7 in the backend review above.

