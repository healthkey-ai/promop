# Source Data Code to Concept Mapping Plan

## Goal

PROMOP already has field mapping: OMOP concepts are mapped to displayable and editable fields on `PatientRecord`. This plan covers the opposite direction for ingest: incoming FHIR and wearable codes must resolve to OMOP concepts before clinical rows are written.

SNOMED and LOINC remain first-class OMOP concepts and do not need local remapping. Non-standard or HealthKey-authored codes are quarantined in local vocabularies such as `HK-Wearable`, `HK-Regimen`, `HK-Drug`, `HK-Observation`, `HK-Procedure`, and future vocabularies such as `HK-Language`.

## Product Behavior

Add a provider-admin page reached by a `Code Mapping` button. The page is a workbench for local/quarantined concepts:

- Show every local quarantined concept, whether or not a source code mapping exists yet.
- Treat concepts as local when they are in an `HK-*` vocabulary or have `Concept.source = 'HealthKey'`, use non-standard OMOP concepts, and live in the local concept id range above the standard OMOP concept id space.
- Show existing source-code mappings currently encoded in the source tree, including `HK-Wearable` concepts.
- Mark local concepts without source mappings as unmapped so curators can edit them and add source codes.
- Let admins create a new mapping from a `New Code` dialog by entering the incoming code, source vocabulary identifier, destination OMOP concept id, destination concept name, and target local vocabulary.
- Let admins choose an existing local vocabulary from a list or type a new `HK-*` vocabulary id.

## Data Model

Add a HealthKey registry table beside OMOP's `source_to_concept_map`.

`SourceCodeConceptMapping` stores curated incoming-code aliases:

- `source_vocabulary_id`: source identifier such as `HK-Wearable`, `Vendor-X`, or `FHIR-Extension-X`.
- `source_code`: incoming code.
- `source_code_description`: optional incoming-code display.
- `target_concept_id`: local/quarantined OMOP concept id.
- `source`: curation provenance, defaulting to `HealthKey`.
- `status`: `active`, `retired`, or `rejected`.
- audit timestamps and reviewer fields.

Uniqueness is enforced on `(source_vocabulary_id, source_code)` so one incoming code has one curated destination.

The list API is concept-centric: it left joins this registry onto every quarantined concept so concepts with no source code still appear. The initial seed only materializes source-code mappings that are already represented in source behavior, starting with `HK-Wearable`; it does not infer that every local concept's `concept_code` is an incoming source code.

## API

Add versioned endpoints guarded by the same staff/org-admin rule as field mapping:

- `GET /api/v1/code-mappings/`: list quarantined concepts with their source mapping, if any.
- `POST /api/v1/code-mappings/`: create a new quarantined concept and source-code mapping.
- `PATCH /api/v1/code-mappings/<concept_id>/`: add or update a source-code mapping for an existing local concept.
- `GET /api/v1/code-mappings/vocabularies/`: list available local vocabularies, domains, and concept classes for the dialog.

The API rejects destination concept ids below `2,000,000,000` and rejects destination vocabularies that are not local `HK-*` vocabularies.

## Implementation Issues

1. Add source-code mapping registry for quarantined concepts.
   Create the Django model, migration, seed from existing local concepts, API endpoints, permissions, and backend tests.

2. Add Code Mapping admin UI.
   Add the route, admin button, table, filters, unmapped rows, create dialog, edit dialog, and frontend tests.

3. Route ingest through the source-code mapping registry.
   Update FHIR and wearable import resolution so non-LOINC/non-SNOMED source codes consult the registry before minting or falling back.

4. Add mapping reconciliation and drift reports.
   Report local concepts without source codes, registry rows whose target concepts disappear, duplicate incoming codes, and source-tree mappings not represented in the registry.

## First Slice

This branch implements issues 1 and 2. Ingest integration and reconciliation are follow-up work because they change import behavior and should be tested separately with representative FHIR and wearable payloads.
