# TODO

## Backend review findings (2026-05-23)

### Critical

#### ~~#1 ServiceTokenAuthentication falls back to arbitrary superuser~~ ✓ FIXED
- `get_or_create(issuer='urn:service', sub='hk-labs-sync')` — dedicated service identity, no superuser fallback.

#### #2 _resolve_person_id allows org-scoped tokens to bypass access check
- **Severity:** critical / security
- `patient_portal/api/lab_results/views.py:70-83`
- When `can_access_patient()` fails but `get_request_org()` returns non-None, the function returns `(pid, None)` granting access. The org-person membership check happens later in each view but the logic is inverted.
- **Action:** Move the org-person membership check into `_resolve_person_id` itself.

#### ~~#3 No rate limiting on auth or write endpoints~~ ✓ FIXED
- `DEFAULT_THROTTLE_CLASSES` (Anon/User/Scoped) + rates (anon: 60/min, user: 300/min, sync: 60/min, patient_sync: 120/min) configured in `settings.py`.

### High

#### ~~#6 EXCLUSIVE table locks per measurement in sync loop~~ ✓ FIXED
- `next_pk_batch()` uses PostgreSQL `nextval` sequences (`omop_core/services/pk.py`). One sequence call allocates all IDs for a batch.

#### ~~#7 Sequential INSERT per measurement instead of bulk_create~~ ✓ FIXED
- `Measurement.objects.bulk_create(new_objects)` at `sync.py:318`.

#### ~~#11 MeasurementDetailView.patch is not atomic~~ ✓ FIXED
- `with transaction.atomic()` wraps `m.save()` + `ProvenanceRecord.objects.create()` at `views.py:629`.

#### ~~#12 VisitDeleteView.delete is not atomic~~ ✓ FIXED
- `with transaction.atomic()` wraps provenance + ownership delete + measurement delete + visit delete at `views.py:721`.

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

#### ~~#18 SyncViewTest uses superuser, masking authorization bugs~~ ✓ FIXED
- Added `SyncNonSuperuserTest` (3 tests: own-data denied, other-person denied, nonexistent denied) and `SyncOnBehalfOfTest` (5 tests covering valid actor, actor-not-found 403, actor-no-access 403, non-superuser 403, superuser-without-actor succeeds).

### Low

#### #16 Provider registry module-level cache without invalidation
- **Severity:** low / python
- `patient_portal/api/providers/registry.py:9-30`
- `_providers` is set once and never cleared. Makes testing difficult.
- **Action:** Add a `clear_providers()` function for tests.

---

## Code review findings (2026-06-26, PR #175 dev→main)

#### #19 _classify_drug fires 3 DB queries per unique drug in LOT inference
- **Severity:** high / performance
- `omop_core/services/lot_inference_service.py:112-136`
- Called once per drug per patient in `_build_drug_eras`. Each drug with a concept_id issues: (1) ConceptRelationship HemOnc mappings, (2) ConceptAncestor ancestor names, (3) Concept direct names — 15–45 round-trips per LOT inference call.
- **Action:** Pre-fetch ConceptRelationship for all drug_concept_ids before the era loop and pass a `hemonc_map` dict into `_classify_drug`.

#### #20 ScopedTokenPermission is method-level only — no built-in object ownership enforcement
- **Severity:** medium / security
- `patient_portal/api/permissions.py:50-66`
- The permission class allows any authenticated patient to PATCH. Object-level ownership (`can_access_patient()`) is enforced in `_ProvenanceMixin.perform_update` and `PatientInfoViewSet.partial_update`, but any new view using `ScopedTokenPermission` without those mixins would allow cross-patient writes.
- **Action:** Add `has_object_permission` override that calls `can_access_patient(request.user, obj.person)`, or document the dependency on `_ProvenanceMixin` in the class docstring.

#### #21 CORS_ALLOWED_ORIGINS silently empty if env var unset in production
- **Severity:** medium / security
- `ctomop/settings.py:226-230`
- When `DEBUG=False` and `CORS_ALLOWED_ORIGINS` env var is absent, the list is empty → all cross-origin requests rejected. App starts without warning; deploy fails silently at the browser.
- **Action:** Add `CORS_ALLOWED_ORIGINS` to the `ImproperlyConfigured` guard block alongside `DATABASE_URL` and `SECRET_KEY`.

---

## Previous findings

### _next_pk holds row locks for entire sync transaction (superseded by #6/#7 above)
- `patient_portal/api/lab_results/sync.py:49-56`
- Superseded by findings #6 and #7 in the backend review above.


