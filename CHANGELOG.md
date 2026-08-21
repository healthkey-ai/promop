# Changelog

All notable changes to PRomop are documented here.

---

## [1.0.0] — 2026-07-04

First stable release. Deployed in production across the HealthTree Foundation and CancerBot,
supporting approximately 17,500 oncology patients and trial matching against 6,000 actively
recruiting trials.

### Core architecture
- OMOP CDM v5.4 PostgreSQL schema as the authoritative clinical record
- `PatientRecord` — 286-column denormalized projection auto-derived via Django signal chain on every OMOP write; reduces a 20-criterion eligibility search from 27–39 joins over raw OMOP to zero
- FHIR R4 Bundle ingestion (`upload_fhir` endpoint + `import_fhir_bundle` management command) mapping observations → `Measurement`, conditions → `ConditionOccurrence`, medications → `DrugExposure` + `Episode`
- Line-of-therapy inference from `DrugExposure` / `Episode` records (ARTEMIS-derived heuristics)

### API
- Versioned REST API at `/api/v1/` with OpenAPI 3.0 schema (`/api/v1/schema/`) and Swagger UI (`/api/v1/docs/`)
- Legacy `/api/` paths retained with `Deprecation` / `Sunset` / `Link` headers (sunset: 2026-09-01)
- `GET /api/v1/patient-records/me/` — patient self-service record access, guarded against auto-provisioning clinical users

### Multi-tenant access control
- Organization model with role-based `GroupAccess` (org admin, doctor, navigator)
- Org invitation flow with email (Mailgun) and OIDC support
- Inter-org trust rules (`OrgTrust`) and public aggregated org statistics
- Service-token ACL bypass for machine-to-machine integrations
- SMART on FHIR authorization + OAuth2 (django-oauth-toolkit)

### Disease-specific extensions (oncology)
- Multiple myeloma: ISS/R-ISS staging, beta-2 microglobulin, FISH cytogenetics, stem cell transplant history/eligibility, therapy line tracking
- Follicular lymphoma: FLIPI, bone marrow involvement, transformation status; HemOnc concept-driven FHIR generator
- Breast cancer: ER/PR/HER2 status, HER2 IHC/ISH, Ki-67, TNM staging
- CLL: CLL-IPI, IGHV mutation status, del(17p)/del(11q)
- Wearable summary fields (step count, resting heart rate, sleep, HRV, SpO2)

### Developer experience
- `generate_fhir_bundle --disease {breast-cancer|mm|fl}` — reproducible synthetic FHIR patient generator
- `import_fhir_bundle` — batch FHIR import with `--org`, `--batch-size`, `--start-from` (resume support)
- Audit log middleware — structured JSON log for every mutating API request
- 640+ backend tests; CI on GitHub Actions (PostgreSQL 16)

## [Unreleased]

---

## [1.1.0] — 2026-08-20

485 commits since 1.0.0. The defining change is architectural: `PatientRecord` became a
**derived read model**. Clinical data is written to OMOP CDM tables and the projection is
rebuilt from those facts; nothing writes the projection directly any more.

### Changed

- **`PatientRecord` is derive-only.** FHIR upload, CSV ingestion, and the OMOP CRUD
  endpoints all write OMOP rows and let the signal chain re-derive the projection. The
  FHIR write-through path was removed, and the derive-only contract is enforced and
  documented.
- **`PatientInfo` renamed to `PatientRecord`** across the backend. The legacy
  `/api/patient-info/` wire format (`patient_info`, `patient_info_id` keys) is unchanged.
- Profile writes moved from `PatientRecord` to `Person`.
- Password minimum raised to 12 characters, matching the analytics (PRism) app.

### Added

- **Bulk OMOP row writes** on the five clinical endpoints, accepting a JSON array with
  one transaction per batch, per-index validation errors, and a 1,000-row cap (#454).
  Idempotent by default: rows upsert on event identity so an ETL re-run or a retry
  converges instead of duplicating.
- **Deferred derivation** — `?skip_refresh=true` on bulk POST and row-level PATCH/DELETE,
  with `POST /api/v1/patient-records/{person_id}/refresh/` to derive afterwards (#532).
- **Patient portal**: first-class patient role, org-scoped invites, patient self-signup,
  consent grants, surveys, messages, allergies, immunizations, and settings (#264, #284).
- **Wearable ingestion** — Apple Watch and Garmin metrics normalized to OMOP rows, upload
  history, 30-day averages, and auto-detection of file type.
- **Vocabulary management** — releases, snapshots with release pinning and
  `X-Vocab-Release-Id` (#371), scope enforcement on release/snapshot endpoints (#344),
  concept graph endpoints (#232), and concept search by name and domain/class.
- **Derivation versioning and per-field OMOP provenance** (#358, #360).
- **PHR-S FM conformance controls** — persisted audit trail with review API (#295),
  retention/archival (#298), standards-based audit format (#303), tamper-evidence,
  hash chaining and break-glass access (#304, #318), authentication controls including
  lockout, no-reuse and forced change (#302, #319), message confidentiality (#308),
  entered-in-error and revision history (#307), FHIR exchange integrity and interchange
  agreements (#306), and terminology maintenance (#305).
- **Standard OMOP CDM 5.4 tables** for conformance, plus line-of-therapy unification with
  per-line outcome persistence in OMOP.
- **Follicular lymphoma pipeline** — Synthea FL generation with realistic timelines and
  mortality (#227), and FL → DLBCL transformation tracking.
- **mCODE FHIR import** support.
- `bulk_import_fhir_bundle` for fast synthetic cohort loading.
- CI now runs the pytest suite as well as the Django runner (#426).

### Fixed

- **Concept integrity**: clinical rows repointed off concepts that used their code as their
  id, `drug_exposure` repointed off locally-minted drug concepts, `Concept.source`
  backfilled, concept 0 metadata corrected, and locally-minted wearable concepts
  quarantined. Stopped minting fake HemOnc concepts (#236).
- CLL ALC unit mismatch in the iwCLL threshold (#544); MM disease-burden labs mapped to the
  LOINC codes real data uses (#537); direct bilirubin LOINC mapping; tumor and lymph-node
  size disambiguated.
- HER2 `Equivocal` receptor results preserved through the projection (#220);
  `valueString` persisted for lab observations (#218); `best_response` persisted (#205);
  `death_date` exposed through the patient_info view.
- Org cascade now deletes all person-FK tables it previously missed.
- 19 of 21 npm security advisories resolved.

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
