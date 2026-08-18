# PRomop Technical Architecture Brief for Utah RHTP / LINCS

Date: 2026-07-09

This brief explains how PRomop maps to Utah's Rural Health Transformation Program
(RHTP), especially the LINCS initiative's call for a "cloud-based, semantically
interoperable data platform" that harmonizes EHR, claims, and public health data
into a single computable structure and exposes it through HL7 FHIR APIs for
analytics, clinical decision support, AI, and patient access.

Relevant source language:
- [Utah-RHTP.pdf](/Users/adamblum/Documents/Utah-RHTP.pdf) pages 39-41
- [Utah-RHTP.pdf](/Users/adamblum/Documents/Utah-RHTP.pdf) page 51

## 1. Executive position

PRomop is already a semantic platform in the architectural sense that matters
most for Utah: it normalizes heterogeneous clinical inputs into a canonical,
patient-level computable model. The platform's `PatientRecord` is the shared
semantic contract. OMOP is the transactional source of truth. FHIR is the
ingestion and exchange surface. Concept tables and mapping rules provide the
terminology normalization layer.

That means Utah does not need to start by building a semantic layer from zero.
The core pattern already exists in PRomop. The practical work is to extend it
from oncology-centric data to statewide rural health data, while broadening the
ingestion sources, governance, and reporting surfaces.

## 2. Current PRomop architecture

PRomop is currently organized around a simple pipeline:

1. Data enters through FHIR R4 bundle uploads and granular OMOP writes.
2. Source data is mapped to OMOP CDM tables.
3. OMOP concept tables are used for terminology resolution.
4. A post-save signal chain derives `PatientRecord`.
5. Downstream applications read from `PatientRecord` and the versioned API.

Key implementation points:
- The platform is OMOP-first and `PatientRecord` is a read model, not the
  system of record. Clinical writes use OMOP APIs or FHIR imports; profile and
  administrative writes use HealthKey extension columns on `Person`. See
  [API_SURFACE.md](/Users/adamblum/promop/API_SURFACE.md).
- `PatientRecord` is a large denormalized projection that combines demographics,
  disease state, therapy lines, labs, biomarkers, behavior, geography, and
  wearable summaries. See [omop_core/models.py](/Users/adamblum/promop/omop_core/models.py#L1210).
- The derivation logic lives in
  [omop_core/services/patient_record_service.py](/Users/adamblum/promop/omop_core/services/patient_record_service.py).
- PatientRecord fields are read-only at the PatientRecord API. Producers write
  complete, provenance-bearing OMOP facts (or FHIR), and the derivation pipeline
  rebuilds those fields. Profile/admin compatibility fields are projected from
  `Person`, so PatientRecord is never a substitute for a source row with its own
  ownership and provenance.
- `public.patient_info` is a legacy SQL compatibility view only. New consumers
  must use `public.patient_record` or the versioned PatientRecord API, and must
  not adopt `patient_info` as a new contract.
- Concept resolution and vocabulary mapping are centralized in
  [docs/concept-mapping.md](/Users/adamblum/promop/docs/concept-mapping.md) and
  [omop_core/services/mappings.py](/Users/adamblum/promop/omop_core/services/mappings.py).

## 3. Why `PatientRecord` is already the semantic contract

Utah's LINCS language is about harmonization, computability, and reuse. A
semantic platform is not defined by a graph database or an ontology tool alone.
It is defined by whether the system presents one stable meaning layer that
multiple producers and consumers can share.

PRomop already does that through `PatientRecord`.

What the model gives Utah:
- A canonical patient representation that hides source-system variability.
- A deterministic projection that can be rebuilt consistently from OMOP.
- A place to aggregate facts from many clinical domains into one patient view.
- A read model suitable for APIs, analytics, decision support, and operations.

This is the practical base of semantic interoperability. Most healthcare systems
expose raw resources. PRomop exposes a normalized patient abstraction.

## 4. Semantic capabilities already present

PRomop already has several semantic foundations that align with LINCS:

### 4.1 Terminology normalization
- LOINC, SNOMED, RxNorm, and HemOnc are used for coding and classification.
- `concept`, `concept_relationship`, and `concept_ancestor` support hierarchy
  and mapping logic.
- `PatientRecord` fields are mapped to standard codes through centralized rules.

### 4.2 Controlled vocabularies
- Oncology and domain-specific picklists are modeled as lookup tables with
  machine-readable codes and human-readable titles.
- The lookup structures already support semantic labels and future governance.

### 4.3 Provenance
- The platform has an explicit `ProvenanceRecord` model for write auditing.
- This is the starting point for broader lineage on derived facts and metrics.

### 4.4 Reusable patient projection
- `PatientRecord` consolidates data into a shared, queryable contract.
- This reduces repeated downstream re-derivation and makes integration easier.

## 5. What Utah still needs for statewide semantic interoperability

PRomop is not complete for the Utah use case yet. To serve LINCS statewide, it
needs the following extensions.

### 5.1 Multi-source ingestion
PRomop should ingest and normalize:
- EHR clinical data
- claims and encounter data
- public health feeds
- patient-generated data
- community partner and care coordination data

### 5.2 Semantic registry
The platform needs a first-class registry for:
- value sets
- concept sets
- phenotype definitions
- transformation rules
- mapping versions
- canonical field definitions

This registry should be versioned and auditable so Utah can track when meaning,
not just schema, changes.

### 5.3 Entity resolution
Statewide data requires durable resolution of:
- person identity
- provider identity
- organization identity
- facility identity
- encounter and episode identity

PRomop already has some of this in its multi-tenant and patient models, but it
needs a broader cross-source identity layer for statewide use.

### 5.4 Lineage on derived facts
For Utah, every derived value should be explainable:
- source system
- source record
- source concept
- transformation rule
- timestamp
- actor or system
- confidence or completeness, where applicable

### 5.5 Population and community reporting
The RHTP evaluation model requires county/community-level reporting and
tracking. That means the semantic layer should support:
- cohort definitions
- longitudinal measures
- county and rural/frontier segmentation
- trend reporting
- quality and access measures

### 5.6 Patient-directed access and consent
Utah's plan explicitly emphasizes secure viewing, download, sharing, and
consent-aware exchange. PRomop needs policy and API support for:
- patient access
- data export
- consent capture and enforcement
- partner-specific sharing rules

## 6. Recommended target architecture

The cleanest model for Utah is a layered architecture:

### Layer 1: Source systems
- EHRs
- HIE feeds
- claims feeds
- public health registries
- patient portals
- device and RPM sources

### Layer 2: Ingestion and normalization
- FHIR ingestion
- batch import pipelines
- claims parsers
- registry adapters
- identity resolution
- terminology resolution

### Layer 3: Canonical clinical store
- OMOP remains the system of record for clinical events and normalized facts.
- Source provenance is retained on every write.

### Layer 4: Semantic patient model
- `PatientRecord` remains the canonical patient contract.
- This layer is where Utah gets computable patient meaning.
- It should be extended with statewide fields, not replaced.

### Layer 5: Semantic services
- terminology search
- value set expansion
- cohort/phenotype evaluation
- relationship traversal
- provenance lookup
- semantic query APIs

### Layer 6: Consumption
- FHIR APIs
- analytic dashboards
- care coordination tools
- patient portal access
- operational reporting
- AI applications

## 7. Why this is the right strategy for Utah

This approach matches the RHTP objectives better than a greenfield semantic
platform because it:

- preserves a real clinical system of record
- avoids re-implementing normalization logic in multiple places
- lets Utah reuse one semantic contract across many workflows
- supports interoperability without losing computability
- creates a practical path from existing infrastructure to statewide scale

The strategic message is simple: PRomop is already doing the hardest part of
semantic interoperability. It turns heterogeneous clinical data into a single,
reusable patient representation. Utah can adopt that pattern as the semantic
core of LINCS and extend it across rural health, public health, and claims.

## 8. Proposed implementation roadmap

### Phase 1: Formalize the semantic contract
- Define `PatientRecord` as the canonical statewide patient model.
- Extract mapping rules into versioned configuration.
- Standardize provenance capture for all derived values.

### Phase 2: Expand source coverage
- Add claims ingestion.
- Add public-health registry ingestion.
- Add community and RPM data feeds.
- Add broader consent and patient-access workflows.

### Phase 3: Add semantic services
- Value set registry
- Phenotype engine
- Semantic search
- Relationship traversal
- Metric registry

### Phase 4: Operationalize statewide use
- County and community dashboards
- Quality reporting
- Care coordination support
- Patient-directed access
- Governance and versioning workflows

## 9. Bottom line

PRomop's `PatientRecord` already gives Utah more than half of the semantic
platform it is asking for. It is the canonical patient abstraction that makes
heterogeneous data computable and reusable. The work ahead is to broaden the
inputs, formalize the semantic registry, strengthen provenance, and add
statewide reporting and access layers.

In short: PRomop is not a point solution. It is the semantic core Utah can build
LINCS around.
