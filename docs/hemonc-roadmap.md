# HemOnc Support Status and Remaining Roadmap

**Status:** Current status + remaining roadmap, updated 2026-08-29  
**Original roadmap date:** 2026-07-21  
**Audience:** PROMOP maintainers; consumers: EXACT, SoC, ht-phr federation host

PROMOP's HemOnc work has moved from "add basic coded therapy support" to "operate as the
governed source of coded therapy and vocabulary data." The original P0/P0b items are
mostly implemented; the remaining work is narrower and should be tracked as focused
issues rather than a broad implementation plan.

Source-code/source-to-concept mapping work is in flight separately in
`source-code-mapping-plan.md`; this document should be updated again after that branch
lands.

## Implemented

### Vocabulary Release and Cache Contract

PROMOP now publishes vocabulary data through a release-oriented API:

- `VocabularyRelease` model with published/staged/retired status
- latest/detail/list endpoints under `/api/v1/vocab-releases/`
- streaming NDJSON snapshot endpoints for:
  - `concept`
  - `concept_ancestor`
  - `concept_class`
  - `concept_relationship`
  - `concept_synonym`
  - `domain`
  - `drug_strength`
  - `relationship`
  - `source_to_concept_map`
  - `vocabulary`
- ETag support for release and snapshot consumers
- `docs/vocab-consumer-cache-protocol.md` describing consumer polling, validation,
  last-known-good behavior, and fail-closed cache activation

Staging has populated `concept_synonym` and `concept_relationship` tables. As of
2026-08-29, published release `9` has row-count/checksum metadata for all snapshot tables.

### Concept and Graph APIs

The concept API includes:

- `/api/v1/concepts/`
- `/api/v1/concepts/search/`
- `/api/v1/concepts/lookup/`
- `/api/v1/concepts/{id}/ancestors/`
- `/api/v1/concepts/{id}/descendants/`
- `/api/v1/concepts/graph/`
- `/api/v1/concepts/{id}/synonyms/`
- `/api/v1/concepts/synonyms/`

Concept responses carry `vocabulary_version`, and concept/synonym search enforces a
minimum query length appropriate for trigram-backed lookup.

### Release Integrity and Namespace Hygiene

FHIR import no longer mints fake `FHIR-*` concepts under `vocabulary_id='HemOnc'`.
Inbound HemOnc regimen ids are validated as current standard HemOnc regimen concepts;
regimen names are matched against HemOnc concepts/synonyms; unmatched names are
quarantined under HealthKey-local vocabulary rows and recorded in `RegimenMappingGap`.

`concept.source` distinguishes external licensed vocabulary rows from HealthKey-local
rows. `report_regimen_mapping_gaps` exposes unresolved regimen names for curation.

### Patient Therapy Projection

The PatientRecord read model now carries:

- first/second/later regimen concept ids
- first/second/later component concept ids
- first/second/later therapy-class/type concept ids
- `therapy_ids_provenance`
- structured `lines_of_therapy[]` in `PatientRecordSerializer`
- aggregate `therapy_release_id` with fail-closed consistency behavior

The structured line payload preserves true line numbers, regimen id/source, component ids,
type ids, dates, outcome, intent, discontinuation reason, and linked editable drug
concepts where an `Episode` exists. Later-line component/type fields are still aggregate
when the underlying read model has no per-later-line component split.

### Curated Therapy Reference Tables

PROMOP has curated therapy reference tables for target diseases and treatment rounds:

- `TherapyRegimen`
- `TherapyComponent`
- `TherapyClass`
- `TherapyRegimenComponent`
- `TherapyComponentClassLink`
- `TherapyRound`
- `DiseaseTherapyRegimen`

These tables are loaded from curated CSVs generated from the therapy spreadsheet and
resolved to standard Athena concepts where possible. They give the therapy-line editor a
disease/round-aware regimen picker with nested component and class data.

Reference endpoints:

```text
GET /api/v1/therapy-regimens/
GET /api/v1/therapy-regimens/{code}/
GET /api/v1/therapy-components/
GET /api/v1/therapy-classes/
```

See `therapy-reference-tables-architecture.md` for the as-built model and API contract.

### LOT Inference

LOT inference uses OMOP vocabulary graph data where available:

- RxNorm-to-HemOnc traversal through `ConceptRelationship`
- HemOnc class lookup through `ConceptAncestor`
- source-value fallback for missing graph evidence
- ARTEMIS-lite/HealthTree segmentation into drug eras, combination windows, and treatment
  episodes

## Remaining Work

### 1. Finish Source-Code / Source-to-Concept Mapping

This is in flight in `source-code-mapping-plan.md`. After it lands, update this document
and issue #236 to reflect what remains around `source_to_concept_map`, local source-code
curation, and release metadata.

### 2. Tighten Release Semantics

The current snapshot API is useful and release-labelled, but vocabulary tables are
current-only. Non-latest snapshot URLs correctly return `409`, so historical manifest
metadata is retained but historical table snapshots are not.

Remaining decisions:

- whether true historical snapshots/deltas are required, or latest-only snapshots are the
  accepted contract
- whether the loader needs a stronger staging-to-publish boundary for production loads
- whether the ADR should narrow "all vocabulary data" to the actual published corpus

Track in #236 and the ADR update issues.

### 3. Ratify ADR 0001

ADR 0001 still needs to match the implemented direction: release-pinned vocabulary mirror,
not ad hoc API result caching. Track in #337 and #254.

### 4. Graph-Based Regimen Resolution

Hardcoded regimen dictionaries still exist as resolution inputs. Replace them as the
source of truth with ambiguity-safe graph/synonym resolution and curated therapy
reference-table data.

Track in #250.

Acceptance:

- no silent first-candidate selection for ambiguous regimens
- unresolved or ambiguous names are surfaced for curation
- curated therapy tables drive disease/round picker behavior
- hardcoded regimen ids become compatibility helpers or are removed

### 5. HemOnc Contexts and Coded Episode Phase

HemOnc Context concepts for first-line, maintenance, second-line, and subsequent-line
therapy are not yet exposed as first-class line-context data. Episode phase is still
encoded mostly as text in `episode_source_value`.

Track in #251.

### 6. Coded Intent and Discontinuation Reason

Intent and discontinuation reason are still string fields in the patient projection.
Add concept companions and include them in `lines_of_therapy[]`.

Track in #252.

### 7. Coded Treatment Outcomes

Outcomes still depend on a small mapping and free-text fallback. Add disease-specific
value sets for RECIST, IMWG, Lugano, and iwCLL outcomes.

Track in #253.

### 8. Consumer Migration

EXACT and SoC still need to consume PROMOP's release-pinned vocabulary mirror and retire
duplicated local HemOnc artifacts or legacy patient-info assumptions. This is partly
outside PROMOP and should be tracked in the consumer repos.

## Issue Map

| Issue | Status | Meaning |
|---|---|---|
| #236 | Open | Umbrella for remaining release/source-of-truth semantics |
| #250 | Open | Graph-based regimen resolution |
| #251 | Open | HemOnc contexts + coded episode phase |
| #252 | Open | Coded intent + discontinuation reason |
| #253 | Open | Coded treatment outcomes |
| #254 | Open | Ratify ADR 0001 |
| #337 | Open | Rewrite ADR 0001 around release-pinned mirror |

## Retired From The Roadmap

These are no longer roadmap gaps:

- component concept ids on PatientRecord
- concept graph API
- concept synonym API
- vocabulary version fields in concept responses
- release manifest/list/latest endpoints
- latest snapshot endpoints
- `source_to_concept_map` model and snapshot surface
- fake HemOnc namespace minting on FHIR import
- derived therapy-id/component fields being writable
- structured `lines_of_therapy[]`
- disease/round-aware curated therapy reference tables
