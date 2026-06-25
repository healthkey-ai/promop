# Changelog

All notable changes to promop are documented here.

---

## [Unreleased]

---

## 2026-06-08

### Fixed
- **Migration 0085 idempotency — AddField** (#128)
  `sct_date` and `sct_eligibility` columns on `patient_info` already existed in production from a prior partial deploy. Wrapped both `AddField` operations in `SeparateDatabaseAndState` with `ADD COLUMN IF NOT EXISTS` SQL so the migration is safe to replay on any database state.

- **Migration 0085 idempotency — CreateModel** (#127)
  `vocabulary_sct_eligibility` table already existed in production. Wrapped `CreateModel` in `SeparateDatabaseAndState` with `CREATE TABLE IF NOT EXISTS` SQL, plus idempotent `DO $$ ... IF NOT EXISTS` blocks for unique constraints.

### Added
- **PHR-ETL integration — Person identity endpoints** (#124)
  Three new API endpoints for the phr-etl data pipeline:
  - `POST /api/persons/find_or_create/` — resolves `(actor_iss, actor_sub)` OIDC identity to a stable `person_id`; auto-provisions on first call; idempotent across organizations.
  - `PATCH /api/persons/{person_id}/` — fill-if-empty demographic patch; only writes fields that are `null` or a recognized placeholder; returns `updated_fields` list.
  - `GET /api/concepts/lookup/?lookup=VOCAB:code` — batch OMOP concept lookup by vocabulary + code pairs.
  - Added `actor_iss` / `actor_sub` fields to `Person` model with partial unique constraint.
  - 18 new backend tests.

---

## 2026-06-07

### Added
- **SCT fields for Multiple Myeloma** (#115)
  Stem cell transplant tracking on `PatientInfo`:
  - `stem_cell_transplant_history` (JSONField) — vocabulary: autologous SCT / allogeneic SCT / tandem SCT.
  - `sct_date` (DateField) — transplant date; future dates rejected by serializer.
  - `sct_eligibility` (JSONField) — new `SctEligibility` vocabulary (eligible/ineligible for autologous/allogeneic SCT); contradictory pairs rejected.
  - FHIR extensions in MM bundle generator + upload handler.
  - `populate_sct_sample_data` management command.
  - `audit_sct_history` management command.

---

## 2026-06-06

### Fixed
- **Dependabot vulnerabilities** (#122)
  Patched react-router open redirect and ws memory disclosure.

### Added
- **Patient surveys, race/MRD fields, MM FHIR bundle, LOT drug classification** (#118)
  Patient survey model and API; race field; MRD (minimal residual disease) field; updated MM FHIR bundle generator; line-of-therapy drug classification.

---

## 2026-06-04

### Fixed
- **Person ID removed from bulk_delete error responses** (#117) — resolves TODO #4.
- **Disease selection persists after save** (#116) — fixes issues #110 and #113.
- **Cross-org email fallback security fix + non-superuser sync auth tests** (#111) — resolves issues #17 and #18.

---

## 2026-06-01

### Fixed
- **OMOP sequence self-heal + ScopedTokenPermission test fix** (#108)
  Auto-repairs out-of-sync PostgreSQL sequences on startup; fixes test fallout from ScopedTokenPermission changes.
