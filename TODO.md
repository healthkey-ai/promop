# TODO

## Backend review findings (2026-05-23)

### Critical

#### ~~#1 ServiceTokenAuthentication falls back to arbitrary superuser~~ ✓ FIXED
- `get_or_create(issuer='urn:service', sub='hk-labs-sync')` — dedicated service identity, no superuser fallback.

#### ~~#2 _resolve_person_id allows org-scoped tokens to bypass access check~~ ✓ FIXED
- Fixed in commit `e2ff378` (PR #163): org membership check moved inside `_resolve_person_id`; returns 403 before `can_access_patient()` is reached on org-scoped tokens.

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

#### ~~#4 Person ID leaked in error response~~ ✓ FIXED
- Error message is now generic `'Person not found.'` (`sync.py:206`).

#### ~~#5 ScopedTokenPermission bypasses scope enforcement for partner auth~~ ✓ FIXED
- service-token → full access; staff/superuser → full access; patients → safe methods + PATCH only (POST/DELETE denied).

#### ~~#8 _get_or_create_hk_concept runs per-measurement without caching~~ ✓ FIXED
- `_preload_hk_concepts` pre-fetches all concept mappings before the loop (`sync.py:272`).

#### ~~#9 Missing db_index on authorization lookup columns~~ ✓ FIXED
- Both `PatientGroupMembership.person_id` and `PersonalRepresentative.person_id` have `db_index=True` (`models.py:222, 317`).

#### ~~#10 resolve_or_create_person race condition on concurrent first-login~~ ✓ FIXED
- `IntegrityError` is caught on `PatientUser.objects.create` and retries lookup (`services.py:66`).

#### ~~#13 MeasurementDetailView.patch uses request.data without serializer~~ ✓ FIXED
- `MeasurementUpdateSerializer` validates `request.data` before writing (`views.py:611`).

#### ~~#14 _hydrate_page fetches ALL measurements then truncates in Python~~ ✓ FIXED
- Uses `ROW_NUMBER() OVER (PARTITION BY concept_id)` window function to limit at DB level (`views.py:278`).

#### ~~#15 _ensure_concept returns None without clear error propagation~~ ✓ FIXED
- Explicit null check returns HTTP 503 with clear message when required concepts are missing (`sync.py:253`).

#### ~~#17 Email fallback in _resolve_person_id can match wrong patient~~ ✓ FIXED
- Email fallback now disabled for non-superuser users without org scope; org-filtered when org present; superusers retain cross-org access.

#### ~~#18 SyncViewTest uses superuser, masking authorization bugs~~ ✓ FIXED
- Added `SyncNonSuperuserTest` (3 tests: own-data denied, other-person denied, nonexistent denied) and `SyncOnBehalfOfTest` (5 tests covering valid actor, actor-not-found 403, actor-no-access 403, non-superuser 403, superuser-without-actor succeeds).

### Low

#### ~~#16 Provider registry module-level cache without invalidation~~ ✓ FIXED
- `clear_providers()` function added to `registry.py:33` for test isolation.

---

## Code review findings (2026-06-26, PR #175 dev→main)

#### ~~#19 _classify_drug fires 3 DB queries per unique drug in LOT inference~~ ✓ FIXED
- Added `_build_hemonc_map()` which pre-fetches all HemOnc relationships in 3 queries total. `_classify_drug` now accepts an optional `hemonc_map` dict; `_build_drug_eras` calls `_build_hemonc_map` once before the era loop and passes it in.

#### ~~#20 ScopedTokenPermission is method-level only — no built-in object ownership enforcement~~ ✓ FIXED
- Added explicit `IMPORTANT — object-level ownership` docstring to `ScopedTokenPermission` documenting the required `_ProvenanceMixin` / `can_access_patient()` pairing for any new view using this permission class.

#### ~~#21 CORS_ALLOWED_ORIGINS silently empty if env var unset in production~~ ✓ FIXED
- `CORS_ALLOWED_ORIGINS` is now included in the `ImproperlyConfigured` guard block in `settings.py`.

---

## Previous findings

### _next_pk holds row locks for entire sync transaction (superseded by #6/#7 above)
- `patient_portal/api/lab_results/sync.py:49-56`
- Superseded by findings #6 and #7 in the backend review above.


