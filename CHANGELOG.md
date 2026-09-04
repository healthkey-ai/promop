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

## [1.2.0] — 2026-09-04

501 commits since 1.1.0. The defining changes are vocabulary governance and a writable
clinical surface: Athena becomes a first-class dependency, source-code-to-concept
mappings gain a reviewable approval pipeline, and the UI can write clinical facts back
into OMOP — not just display a derived projection.

### Upgrade requirement — load the Athena vocabularies before deploying

```bash
python manage.py load_athena_vocabularies --path /path/to/athena --concepts-only
```

Clinical foreign keys resolve against loaded vocabulary. **Without this load** the
writable-field descriptor reports no editable fields, demographic corrections save
as text with a null concept, and derivation silently reads nothing.

`--concepts-only` skips `concept_relationship`, `concept_ancestor`,
`concept_synonym` and `drug_strength` — the difference between an 11-second load
and streaming ~26M rows to add a handful of concepts.

Newly in `VOCAB_SCOPE`, all of which were in the Athena bundle all along and
simply never loaded:

- **`Gender`, `Race`, `Ethnicity`** — `Person.race_concept` could not previously
  hold a real concept on any deployment.
- **`Episode`, `CDM`** — `32531` Treatment Regimen (what line-of-therapy episodes
  point at) and `1147094` `drug_exposure.drug_exposure_id` (referenced by
  `EpisodeEvent`). Both were hand-seeded; Athena supplies them with identical ids,
  names and codes.

### Removed

- **`seed_omop_concepts` is removed.** It maintained 99 concepts by hand, 97 of
  which Athena already supplies. Offering it as an operator command made it a
  competing source of truth for concepts, which is how a locally invented concept
  ends up occupying an id the vocabulary owns — what happened with `3000963`,
  turning every unmapped lab into a haemoglobin result and leaving 19 staging
  patients with a haemoglobin of 1.0 g/dL. The data survives as
  `omop_core/concept_fixtures.py`, imported only by tests that need concepts
  without a 4.6 GB Athena bundle. Locally-minted `HK-Wearable` concepts arrive
  via migration 0143.

### Added

- **Source code concept mapping (SCCM) framework** — a reviewable, approvable
  pipeline for mapping source codes to standard OMOP concepts. Doctors and
  analysts propose mappings; administrators approve them. Unapproved mappings do
  not enter the clinical record (#820, #830, #834, #848, #849, #856, #872, #875).
- **Code mapping administration hub** — visual interface with approval queues,
  role-based gates, source vocabulary tabs (Athena sync, CR mirroring), batch
  ETL crossmap import, and mapping coverage statistics (#894, #896, #898).
- **Multiple source vocabulary imports** — UMLS/MRCONSO direct import with
  streaming loads (#983), HK-Labs curated LOINC mappings auto-approved on deploy
  (#941), HealthTree FHIR crossmaps (#891, #912), CureHub FHIR crossmaps (#924),
  and OpenWearables vocabulary (#909).
- **Batch concept lookup** — `GET /api/concepts/lookup/` resolves vocabulary + code
  pairs in bulk, so an ETL pipeline does not need one round-trip per code.
- **Auto-suggest mappings** — unmapped fields get suggested mappings based on name
  similarity, concept domain, and vocabulary context (#682, #856).
- **Field mapping transfer** — `copy_field_mappings` management command copies
  curated mappings between PRomop instances. Matching is by natural key, concept
  FKs are re-resolved by `(vocabulary_id, concept_code)`, and reviewer attribution
  is cleared (#981).
- **UMLS and vocabulary release management** — release caching and pinning (#980),
  direct vocabulary release publishing, nested archive support, and streaming UMLS
  imports without full-archive caching.
- **Writable-field descriptor endpoint** — clients query which fields are writable,
  what concepts they accept, and how to write them as OMOP facts (#605).
- **Writable demographics** — Gender, Race, and Ethnicity are now correctable with
  coded OMOP concepts, not free text. Six location fields updatable through the
  Person endpoint (#608, #609).
- **Labs tab writes OMOP Measurement facts** directly, with concept resolution and
  unit normalization, driven by the writable-field descriptor (#602).
- **Blood tab writes OMOP facts** for hematology values through the same
  descriptor-driven pipeline (#622, #955).
- **Disease, Behavior, and Wearable tabs** rendered from the field descriptor, so
  new fields appear in the UI without frontend changes (#645, #647, #649, #651).
- **Treatment tab** — clinicians author lines of therapy through the API with
  structured regimen references, drug class categorization, and a disease-filtered
  regimen picker (#637, #639, #641, #672).
- **Therapy reference tables** — curated CSV-seeded therapy and component reference
  data, regimen picker filtered by disease, drug class categorization, and
  line-of-therapy metadata (#763–#767, #775, #776).
- **Custom patient fields** — foundation for organization-specific patient
  attributes, so each deployment can extend the record without forking the
  schema (#727).
- **Field mapping enhancements** — concept mapping interface for PatientRecord
  fields (#595, #616), tabbed layout with synonym management (#624), compound
  field mappings (#674), curated field mapping units (#689, #704), field formulas
  with derivation (#675).
- **Async derivation with Celery** — `?skip_refresh=true` suppresses PatientRecord
  rebuild on bulk POST and row-level PATCH/DELETE; `POST
  /api/v1/patient-records/{person_id}/refresh/` queues derivation on Celery,
  returns `202 Accepted` with a task ID. Inline dispatcher for dev without Redis.
  25-second statement timeout on both paths. Signed inline task IDs. Status
  polling at `/api/v1/derivation-status/{task_id}/` (#678).
- **FHIR DocumentReference ingestion** — clinical documents attached to a patient
  record are captured in OMOP (#569).
- **FHIR observation ranges and interpretation** — reference ranges and abnormal
  flags from the source system are mapped and stored (#562).
- **FHIR observation deduplication by concept** — duplicate observations from
  multi-provider patients are collapsed rather than stacked (#561).
- **Skipped FHIR resource reporting** — resources that cannot be ingested are
  logged with the reason, creating an audit trail for data completeness (#570).
- **Clinical OMOP list pagination** on the five clinical endpoints (#564).
- **Clinical list filters** for conditions, measurements, observations, drug
  exposures, and procedures (#565).
- **Single-POST upsert** — row-level clinical POSTs now upsert on event identity,
  matching the bulk path (#567).
- **Clinical provenance idempotency** — re-synced clinical writes do not duplicate
  provenance records (#558).
- **Apple/Garmin wearable code mappings** — curated mappings managed through the
  SCCM approval workflow, with database-driven ingest configuration (#923, #951).
- **Language skills** — a patient's language capabilities (speak, read, write, sign)
  are settable from the API and UI, coded with HK-Language concepts, and flattened
  for matching (#808, #813, #821).
- **Organization-scoped signup** — patients can self-register and associate with an
  organization through an email-filtered invitation flow (#572, signup filters).
- **Pending organization invitations** visible on user profile.
- **Sign Up tab** on the homepage login page (#572).
- **Vocabulary load from Google Drive** — `load_athena_vocabularies --gdrive` for
  deployments that store the Athena bundle on GDrive (#631).
- **Validation fields modelled as person equivalences** (#783).
- **`load_mappings` command** — integrated with `load_athena_vocabularies` for
  deploying mapping artifacts alongside vocabulary loads (#977).
- **HK-Labs SCCM seed migration** — migration `0201` seeds approved HK-Labs-to-LOINC
  mappings; deployment gate in `start.sh` enforces Athena load before applying (#972).
- **Tab-field overlap ESLint rule** — prevents a field from rendering an editable
  box on two tabs; replaces the runtime test (#955).

### Changed

- **Value concepts coded, not just questions** — observation and measurement
  answers resolve to coded concepts, not just source text (#774, #723).
- **Refresh prefetch** — OMOP rows prefetched to eliminate 504s on large patients
  during PatientRecord derivation (#541).
- **Refresh snapshot reuse** — LOT inference reuses the refresh snapshot instead of
  re-querying (#617).
- **Concept-zero repointing** — unambiguous concept-zero clinical rows safely
  repointed to the correct concept, gated on source provenance (#846).
- **Destructive vocabulary replacement blocked** when patient data references
  the vocabulary (#681).
- React pinned to 19.2.6 (#pin-react-19.2.6).
- DRF bumped from 3.15.2 to 3.17.2 for CVE fixes.
- Tailwind preflight scoped to stop leaking into the federation host document.
- Theme vars no longer written as inline styles on the root element.
- Docker stack made configurable and able to start with `DEBUG` off (#553).
- Fixed database-name portability: removed hardcoded `ctomop` assumptions (#551).

### Fixed

- **Unmapped-lab concept collision** — stopped the unmapped-lab fallback from
  occupying a real concept's id, which turned every unmapped lab into a false
  haemoglobin result (#599).
- **BMI derivation** for metre-height inputs (#769).
- **Regimen naming** — stopped a combination from being named after one of its
  drugs (#642).
- **Upsert timezone normalization** — upsert key datetimes normalized to avoid
  timezone-aware/naive mismatches creating duplicates (#531, #533).
- **Superseded-row upsert match** — a superseded row is no longer matched by a
  later identical write (#649 followup).
- **Projection patch** — sending an edit no longer drops derived fields from the
  save payload (#627).
- **Domain box overflow** — domain box value no longer bleeds into adjacent fields
  in the code mapping UI (#928).
- **Org cascade** — `manage_language_skills` stopped minting concepts and matching
  by name (#812).
- **Audit log** — deprecation warnings use a separate logger to avoid polluting
  the structured audit log (#879).
- **Clinical session auth** enforced by role (#555).
- **Clinical query filter validation** — unknown filters rejected with 400 (#563).
- **CORS provenance headers** — `X-Provenance-Source` and `X-Provenance-User-Id`
  allowed through CORS (#554).
- **M-Protein type values** updated (#811).
- **CLL/DLBCL disease string variants** handled in the regimen picker (#798).
- **Frontend lint regressions** — `set-state-in-effect` violations fixed in the
  field-mapping load (#630) and synonym dialog (#638).
- **P0 security audit findings** closed (#756): token cache expiry (#759),
  signing-key work (#749).
- **Boolean assertion coercion** wired into Measurement/Observation serializers
  (#881).
- Multiple migration graph conflicts resolved: merged migration heads at 0154,
  0176, 0177, 0178, 0179, 0184, 0185, 0200.

### Migration notes

| Item | Details |
|------|---------|
| Migration endpoint | `omop_core.0201_seed_hklabs_sccm` |
| Total migrations | 202 (up from 201 in v1.1.0) |
| Order requirement | Migrate through 0200, load Athena vocabularies, then apply 0201 |
| Vocabulary load time | ~11 seconds (`--concepts-only`) |
| Deployment gate | `start.sh` enforces Athena load before applying 0201 |
| Breaking changes | 1 — `seed_omop_concepts` removed, replaced by Athena vocabulary load |

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
