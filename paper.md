---
title: 'PRomop: A Longitudinal Decision-Ready Patient Health Record Built on OMOP CDM and FHIR R4'
tags:
  - Python
  - Django
  - OMOP CDM
  - FHIR
  - clinical trials
  - patient registry
  - electronic health records
  - oncology informatics
authors:
  - name: Adam Blum
    orcid: 0009-0009-4985-7615
    email: adam@healthkey.ai
    affiliation: 1
affiliations:
  - name: HealthKey AI
    index: 1
date: 4 July 2026
bibliography: paper.bib
---

# Summary

Health systems and biopharma face a persistent gap between holding patient data and acting on it.
Records remain fragmented across providers, standards-conformant but manually mapped, and
structured for storage rather than decision-making. Every downstream application — analytics,
trial matching, clinical decision support — re-derives patient clinical state from scratch,
multiplying effort and inconsistency.

PRomop is an open-source longitudinal patient health record built on the OMOP Common Data Model
(CDM 5.4) [@OHDSI2021] with oncology extensions. Its keystone is `PatientRecord`, a flattened,
denormalized projection that collapses each patient's complete longitudinal history into a single
decision-ready row of 286 columns. While the transactional CDM tables preserve everything that
ever happened, `PatientRecord` represents what is true *now* — computing patient-state derivations
once rather than repeatedly per consumer. Population analytics, clinical trial matching, and
standard-of-care evaluation all operate on one shared substrate rather than maintaining divergent
copies of the truth.

PRomop is deployed in production across the HealthTree Foundation (14,000 blood-cancer patients)
and CancerBot (3,500 patients), supporting trial matching against 6,000 actively recruiting
trials across five cancer types. A 20-criterion eligibility search that requires 27–39 joins over
raw OMOP reduces to zero joins against the projection — an estimated 30–200× speedup.

# Statement of Need

PRomop is designed for clinical informaticists, data scientists, and developers who need to build
or integrate with a longitudinal patient health record — whether to power a trial matching engine,
construct feature sets for clinical ML models, or deploy patient-level clinical decision support.

The foundational standards address complementary but distinct problems and together do not fill
this need. OMOP CDM provides a normalized, vocabulary-mapped schema optimized for population-level
observational research. OHDSI tools built on it — ATLAS, HADES, ACHILLES — excel at cohort
definition and epidemiological analysis across large databases [@OHDSI2021; @Overhage2012], but
operate at the population level and do not provide a queryable per-patient state. Answering "what
is this patient's current disease status, most recent lab values, and prior therapy lines?"
requires assembling and aggregating across multiple OMOP tables at query time; every application
that needs this must re-implement the derivation independently.

FHIR R4 is an exchange protocol: it defines how clinical data moves between systems, not how it
is stored for analytical queries [@HL7FHIR; @Mandel2016]. A FHIR server preserves resources in
their original form; determining a patient's current clinical state still requires traversing and
reconciling multiple resource types. Neither standard, nor the tools built on them, produces a
pre-computed, per-patient record ready for trial matching criteria evaluation, CDS rule firing, or
use as an ML feature vector.

The consequence is repeated, redundant derivation. Each downstream application independently
reconstructs patient state — resolving lines of therapy, determining current disease status,
normalizing biomarkers, reconciling conflicting source values. This re-derivation is expensive,
error-prone, and a frequent source of inconsistency between applications that should agree.

PRomop fills this gap by:

- Storing records in OMOP CDM 5.4, inheriting compatibility with the OHDSI ecosystem
  [@OHDSI2021]
- Accepting FHIR R4 Bundle uploads that map directly into OMOP tables (observations →
  `Measurement`, conditions → `ConditionOccurrence`, medications → `DrugExposure` + `Episode`)
- Automatically deriving `PatientRecord` via a signal chain whenever any underlying OMOP record
  changes, so downstream consumers never reconstruct state themselves
- Exposing a versioned REST API (`/api/v1/`) with an OpenAPI 3.0 schema for integration with
  trial matching, CDS, and analytics services
- Providing a React-based clinician interface and synthetic FHIR data generators for each
  supported disease, enabling fully offline development and reproducible testing

# Software Description

## Architecture

PRomop separates a standards-based transactional record from a decision-ready projection.

```
FHIR R4 Bundle ingest
        │
        ▼
OMOP CDM tables  (Measurement, ConditionOccurrence, DrugExposure, Episode …)
        │  post_save signal chain
        ▼
  PatientRecord  (286-column denormalized projection, one row per patient)
        │
        ├── Population analytics (PRism)
        ├── Clinical trial matching (EXACT)
        └── Standard-of-care evaluation
```

All clinical data is written to normalized OMOP tables. `PatientRecord` is derived automatically
on every write and materialized in PostgreSQL. Every consuming application reads from
`PatientRecord`; none reconstructs patient state independently.

## PatientRecord: Decision-Ready Projection

`PatientRecord`'s 286 columns include demographics, current staging, lines of therapy, lab
values, biomarkers, and derived clinical states. A representative 20-criterion eligibility
search over raw OMOP requires 27–39 joins (laboratory criteria alone demand correlated subqueries
to recover the most-recent value per test). Against `PatientRecord`, the same query is a flat
predicate over a single table: zero joins, zero aggregation (Table 1).

| Approach | Joins | Est. time (10k patients) |
|---|---|---|
| Raw OMOP, 20 criteria | 27–39 | 15–120 s |
| PatientRecord, 20 criteria | 0 | 50–500 ms |
| **Effective speedup** | | **30–200×** |

Table 1: Estimated query cost for a 20-criterion eligibility search (10,000 patients).
Estimates are analytical, derived from OMOP table cardinality and query structure.

## Key Components

**OMOP data model** (`omop_core/models.py`): OMOP CDM 5.4 entities plus `PatientRecord` and
disease-specific vocabulary lookup tables, extensible beyond oncology.

**FHIR ingestion** (`patient_portal/api/views.py`): Maps FHIR R4 Bundle entries to OMOP tables
using LOINC, SNOMED CT, and RxNorm; falls back to source value strings where standard codes are
unavailable.

**Line-of-therapy inference** (`omop_oncology/`): Derives structured therapy line records from
`DrugExposure` and `Episode` data, supplementing the ARTEMIS approach [@Golozar2023] with
domain-specific rules.

**Versioned REST API**: Stable `/api/v1/` surface with OpenAPI 3.0 schema and Swagger UI.
OAuth2 and SMART on FHIR authorization [@Mandel2016].

**Synthetic data generation** (`generate_fhir_bundle --disease {breast-cancer|mm|fl}`):
Reproducible FHIR R4 Bundles for development and testing without access to real patient data.

## Deployment and Scale

PRomop is deployed across two independent organizations totaling approximately 17,500 patients.
Trial matching covers 6,000 actively recruiting trials across five cancer types.

# Acknowledgements

Thank you to HealthTree Foundation for funding and feedback. Thank you to advisors Steve Labkoff
and Yuri Quintana for guidance and insight. The OMOP CDM vocabulary and concept infrastructure
is maintained by the OHDSI community. FHIR R4 specifications are published by HL7 International.

# References
