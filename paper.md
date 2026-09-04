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
  - given-names: Adam
    surname: Blum
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

Caring for a patient produces a long trail of separate records: a lab result one week, a diagnosis
the next, a prescription from a different clinic, often stored in different systems and different
formats. Answering an ordinary clinical question — *is this patient's cancer currently responding
to treatment? what was their most recent blood count? which drug regimens have they already
tried?* — means collecting those scattered pieces and working out what they add up to. Today every
piece of software that needs such an answer redoes that work for itself, and two programs looking
at the same patient often disagree.

PRomop is open-source software that does this assembly once, in advance, and saves the result.
Incoming records are stored in the OMOP Common Data Model (CDM 5.4) [@OHDSI2021] — the database
layout used by the OHDSI observational-research community — extended with fields specific to
cancer care. Like most clinical databases, OMOP spreads a patient's history across many tables,
one per kind of event: all lab results in one table, all diagnoses in another, all medications in
a third. That layout is excellent for recording faithfully what happened, but awkward for asking
what is true about a patient *right now*, because the answer has to be reassembled from many
tables every time it is needed.

PRomop adds a table called `PatientRecord` that holds one wide row per patient — over 300
columns — containing the current best answer for each clinical fact: disease stage, most recent
lab values, current line of therapy, and so on. The row is *flattened* (or *denormalized*): facts
that would normally have to be gathered by joining many tables are pre-computed and copied into a
single place, so a program can simply read the answer instead of deriving it. Whenever new
clinical data arrives, PRomop updates the affected parts of that row automatically. The
underlying OMOP tables remain the complete historical record of everything that ever happened;
the `PatientRecord` row records what is true now. Analytics dashboards, clinical trial matching,
and treatment-guideline checking then all read the same pre-computed answers instead of each
maintaining its own version of the truth.

PRomop is deployed in production across the HealthTree Foundation (14,000 blood-cancer patients)
and CancerBot (3,500 patients), supporting trial matching against 6,000 actively recruiting
trials across five cancer types. Checking whether a patient meets a trial's eligibility criteria
runs about 37 times faster against `PatientRecord` than against the raw OMOP tables
[@Blum2026].

# Statement of Need

PRomop is designed for clinical informaticists, data scientists, and developers who need to build
or integrate with a longitudinal patient health record — a record covering a patient's full
history over time rather than a single visit — whether to power a trial matching engine,
construct feature sets for clinical ML models, or deploy patient-level clinical decision support.

Today, answering "what is this patient's current disease status, most recent lab values, and prior
therapy lines?" requires assembling and aggregating across multiple tables at query time. Every
downstream application — analytics dashboards, trial matchers, CDS engines — independently
reconstructs patient state: resolving lines of therapy, determining current disease status,
normalizing biomarkers, reconciling conflicting source values. This re-derivation is expensive,
error-prone, and a frequent source of inconsistency between applications that should agree.
Neither OMOP CDM's normalized tables nor FHIR's resource-oriented exchange protocol (see *State
of the Field*) produces a pre-computed, per-patient record ready for these use cases.

PRomop fills this gap by:

- Storing records in OMOP CDM 5.4, inheriting compatibility with the OHDSI ecosystem
  [@OHDSI2021]
- Accepting FHIR R4 [@HL7FHIR] Bundle uploads that map directly into OMOP tables (observations →
  `Measurement`, conditions → `ConditionOccurrence`, medications → `DrugExposure` + `Episode`)
- Automatically deriving `PatientRecord` via a signal chain whenever any underlying OMOP record
  changes, so downstream consumers never reconstruct state themselves
- Exposing a versioned REST API (`/api/v1/`) with an OpenAPI 3.0 schema for integration with
  trial matching, CDS, and analytics services
- Providing a React-based clinician interface and synthetic FHIR data generators for each
  supported disease, enabling fully offline development and reproducible testing

# State of the Field

Several tools address parts of the problem PRomop targets, but none combines OMOP-native storage,
FHIR ingestion, and a pre-computed per-patient projection in a single open-source package.

**OHDSI ATLAS / HADES / ACHILLES** [@OHDSI2021; @Overhage2012] are the reference tools for
population-level observational research on OMOP CDM. They excel at cohort definition and
epidemiological analysis but operate at the population level; they do not produce a queryable
per-patient clinical state suitable for trial matching or point-of-care CDS.

**TrialGPT** [@Jin2024] applies large language models to match patients to trials from
unstructured text. It bypasses structured data entirely, trading reproducibility and auditability
for flexibility. PRomop takes the complementary approach: structured, vocabulary-mapped data with
deterministic derivation.

**HAPI FHIR** [@HAPIFHIR] and other FHIR servers store and serve FHIR resources faithfully but do not map
them into a common analytical schema. Querying current patient state still requires traversing
and reconciling multiple resource types.

PRomop's contribution is the `PatientRecord` projection layer: a single denormalized row per
patient, derived automatically from OMOP writes, that eliminates repeated state reconstruction
across consuming applications.

# Software Design

PRomop's central design trade-off is **normalization for writes versus denormalization for reads**.
Clinical data arrives as FHIR R4 Bundles and is written into normalized OMOP CDM 5.4 tables
(`Measurement`, `ConditionOccurrence`, `DrugExposure`, `Episode`), preserving full longitudinal
history and OHDSI-ecosystem compatibility. A Django `post_save` signal chain then derives
`PatientRecord` — a wide, pre-computed summary table (currently over 300 columns), one row per
patient, storing values that would otherwise be recalculated from the OMOP tables on every
query — on every write.

```
FHIR R4 Bundle ingest
        │
        ▼
OMOP CDM tables  (Measurement, ConditionOccurrence, DrugExposure, Episode …)
        │  post_save signal chain
        ▼
  PatientRecord  (300+ column denormalized projection, one row per patient)
        │
        ├── Population analytics (PRism)
        ├── Clinical trial matching (EXACT)
        └── Standard-of-care evaluation
```

This design accepts higher write cost (the projection must be refreshed on each update) in
exchange for dramatically lower read cost. A representative 20-criterion eligibility search
over raw OMOP requires 27–39 joins; against `PatientRecord` it is a flat predicate over a
single table. Benchmarks on a synthetic breast-cancer cohort measured a 37× speedup (0.30 ms
vs. 11.0 ms per patient) for eligibility screening [@Blum2026] (\autoref{tab:benchmark}).

| Approach | Joins | Time per patient |
|---|---|---|
| Raw OMOP, 20 criteria | 27–39 | 11.0 ms |
| PatientRecord, 20 criteria | 0 | 0.30 ms |
| **Measured speedup** | | **~37×** |

: Eligibility screening cost per patient (synthetic breast-cancer cohort) [@Blum2026]. []{label="tab:benchmark"}

Key implementation components include:

- **FHIR-to-OMOP ingestion** mapping LOINC, SNOMED CT, and RxNorm codes into OMOP tables with
  fallback to source value strings
- **Line-of-therapy inference** supplementing the ARTEMIS approach [@Golozar2023] with
  domain-specific rules for oncology regimen detection
- **Synthetic data generators** producing reproducible FHIR R4 Bundles per disease type for
  fully offline development and testing
- **Versioned REST API** (`/api/v1/`) with OpenAPI 3.0 schema, OAuth2, and SMART on FHIR
  authorization [@Mandel2016]

# Research Impact Statement

PRomop is deployed in production across two independent organizations — the HealthTree Foundation
(approximately 14,000 blood-cancer patients) and CancerBot (approximately 3,500 patients) —
totaling roughly 17,500 patient records. These deployments support clinical trial matching against
6,000 actively recruiting trials across five cancer types (multiple myeloma, follicular lymphoma,
chronic lymphocytic leukemia, breast cancer, and diffuse large B-cell lymphoma).

The `PatientRecord` projection has enabled integration with two downstream systems: PRism, a
population analytics dashboard, and EXACT, a clinical trial matching engine. Both consume the
same pre-computed patient state rather than independently deriving it, eliminating a class of
inconsistency bugs between applications that previously disagreed on patient status. A companion
paper [@Blum2026] provides architectural details and empirical benchmarks of the projection
approach.

The software is openly available, includes synthetic FHIR data generators for each supported
disease type, and can be deployed without access to real patient data — lowering the barrier for
researchers and informaticists to adopt, extend, or benchmark against their own clinical
data pipelines.

# AI Usage Disclosure

Generative AI tools — primarily Anthropic Claude (via Claude Code) and GitHub Copilot — were used
extensively throughout this project. AI assisted with software development (code generation,
debugging, test authoring, and code review), documentation drafting (including portions of this
paper), and architectural exploration. All AI-generated content was reviewed, tested, and edited
by the author. Automated test suites (backend and frontend) were run against all code changes
regardless of origin. The author accepts full responsibility for the correctness and scholarly
integrity of the final software and manuscript.

# Acknowledgements

Thank you to HealthTree Foundation for funding and feedback. Thank you to advisors Steve Labkoff
and Yuri Quintana for guidance and insight. The OMOP CDM vocabulary and concept infrastructure
is maintained by the OHDSI community. FHIR R4 specifications are published by HL7 International.

# References
