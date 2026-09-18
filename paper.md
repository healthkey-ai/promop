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
date: 18 September 2026
bibliography: paper.bib
---

# Summary

Caring for a patient produces a long trail of separate records — a lab result one week, a
diagnosis the next, a prescription from a different clinic — often in different systems and
formats. Answering an ordinary clinical question — *is this patient's cancer currently responding
to treatment? what was their most recent blood count? which drug regimens have they already
tried?* — means collecting those scattered pieces and working out what they add up to. Today every
program that needs such an answer redoes that work itself, and two programs looking at the same
patient often disagree.

PRomop is open-source software that does this assembly once, in advance, and saves the result.
Incoming records are stored in the OMOP Common Data Model (CDM 5.4) [@OHDSI2021] — the database
layout used by the OHDSI observational-research community — extended for cancer care. OMOP
spreads a patient's history across many tables, one per kind of event: lab results in one,
diagnoses in another, medications in a third. That layout records faithfully what happened, but
is awkward for asking what is true about a patient *right now*, because the answer has to be
reassembled from many tables every time.

PRomop adds a table called `PatientRecord` that holds one wide row per patient — over 300
columns — containing the current best answer for each clinical fact: disease stage, most recent
lab values, current line of therapy, and so on. The row is *denormalized*: facts that would
normally be gathered by joining many tables are pre-computed into a single place, so a program
reads the answer instead of deriving it. When new clinical data arrives, PRomop recomputes the
row automatically. The OMOP tables remain the complete history; the `PatientRecord` row records
what is true now. Analytics dashboards, clinical trial matching, and treatment-guideline checking
all read the same answers instead of each maintaining its own version of the truth.
On a synthetic 1,000-patient cohort, checking a patient against a trial's eligibility criteria
runs about 37 times faster against `PatientRecord` than against the raw OMOP tables.

# Statement of Need

PRomop is designed for clinical informaticists, data scientists, and developers who need to build
or integrate with a longitudinal patient health record — a record covering a patient's full
history over time rather than a single visit — whether to power a trial matching engine,
construct feature sets for clinical machine-learning models, or deploy patient-level clinical
decision support (CDS).

Today every downstream application independently reconstructs patient state at query time:
resolving lines of therapy, determining current disease status, normalizing biomarkers,
reconciling conflicting source values. This re-derivation is expensive, error-prone, and a
frequent source of inconsistency between applications that should agree. Neither OMOP CDM's normalized tables nor FHIR's
resource-oriented exchange protocol (see *State of the Field*) produces a pre-computed,
per-patient record ready for these use cases.

PRomop fills this gap by:

- Storing records in OMOP CDM 5.4 with the OMOP Oncology extension [@Belenkaya2021],
  inheriting compatibility with the OHDSI ecosystem
- Accepting FHIR R4 [@HL7FHIR] Bundle uploads, including mCODE-conformant oncology bundles
  [@Osterman2020], that map directly into OMOP tables (observations → `Measurement`,
  conditions → `ConditionOccurrence`, medications → `DrugExposure` + `Episode`)
- Deriving `PatientRecord` automatically whenever underlying OMOP data changes, so downstream
  consumers never reconstruct state themselves
- Exposing a versioned REST API (`/api/v1/`) with an OpenAPI 3.0 schema
- Providing a React clinician interface and synthetic FHIR data generators for each supported
  disease, enabling offline development and reproducible testing

# State of the Field

Several tools address parts of this problem; none combines OMOP-native storage, FHIR ingestion,
and a pre-computed per-patient record.

**OHDSI ATLAS / HADES / ACHILLES** [@OHDSI2021; @Overhage2012] are the reference tools for
population-level observational research on OMOP CDM. They answer questions about populations; they do
not maintain a queryable per-patient clinical state for trial matching or point-of-care CDS.

**FHIR servers and FHIR–OMOP bridges.** HAPI FHIR [@HAPIFHIR] stores and serves FHIR resources
faithfully but does not map them into an analytical schema. OMOPonFHIR [@OMOPonFHIR] goes
the other way, exposing an OMOP database through a FHIR API. Both translate between
representations of the *event history*; with either, current patient state must still be
reconstructed by the caller.

**Trial matchers.** Criteria2Query [@Yuan2019] turns eligibility text into OMOP cohort queries,
and MatchMiner [@Klein2022] matches patients to trials on genomic and clinical criteria from its
own data model. TrialGPT [@Jin2024] applies large language models to unstructured patient text,
trading reproducibility and auditability for flexibility. Each is a *consumer* of patient state
and each derives that state itself.

**Build versus contribute.** `PatientRecord` could not be contributed to these projects without
changing what they are. The OMOP CDM is deliberately a normalized research schema loaded by batch
ETL; a continuously maintained summary row per patient, with oncology-specific derivation rules,
is outside its scope and that of ATLAS.
FHIR servers and bridges are format translators with no place for clinical derivation logic, and
adding it to a single trial matcher would reproduce the duplication PRomop removes. PRomop
therefore builds *on* these standards — OMOP tables and vocabularies, FHIR and mCODE for
ingestion, SMART on FHIR for authorization — and contributes the one missing layer.

# Software Design

PRomop's central design trade-off is **normalization for writes versus denormalization for reads**.
Clinical data arrives as FHIR R4 Bundles and is written into normalized OMOP CDM 5.4 tables,
preserving full longitudinal history and OHDSI compatibility. `PatientRecord` is then derived
from those tables and is never written directly by ingestion.

```
FHIR R4 Bundle ingest
        │
        ▼
OMOP CDM tables  (Measurement, ConditionOccurrence, DrugExposure, Episode …)
        │  derivation (on write, or queued)
        ▼
  PatientRecord  (300+ columns, one row per patient)
        │
        ├── population analytics
        ├── clinical trial matching
        └── standard-of-care evaluation
```

Three alternatives were weighed. *Query-time SQL views* are never stale but pay the full join
cost on every read. *Database materialized views* cannot express the derivation: line-of-therapy inference, unit normalization, and vocabulary lookups are
procedural, and a materialized view refreshes for every patient at once rather than for the one
who changed. A *FHIR-native store* would give up the OMOP vocabularies and the OHDSI tooling.
PRomop therefore derives in application code (Django [@Django]), per patient.

This accepts a higher write cost for a much lower read cost. A representative 20-criterion
eligibility check over raw OMOP requires 27–39 joins; against `PatientRecord` it is a predicate
over one row (\autoref{tab:benchmark}).

| Approach | Joins | Median time per patient |
|---|---|---|
| Raw OMOP, 20 criteria | 27–39 | 11.0 ms |
| PatientRecord, 20 criteria | 0 | 0.30 ms |
| **Speedup** | | **~37×** |

: Eligibility screening cost per patient on a 1,000-patient synthetic breast-cancer cohort (about 217,000 measurement rows). []{label="tab:benchmark"}

The cohort was generated with Synthea [@Walonoski2018], contains no real patient data, and is
archived [@BlumCohort2026]. `docs/reproducing-benchmark-results.md` in the repository gives the
commands to load it and re-run the measurement. The ratio is of medians (about 36× on means), and
it grows with cohort size, because the raw-OMOP path searches a growing `Measurement` table while
the `PatientRecord` path remains a single indexed row read; a 100-patient cohort gives a smaller
ratio. A companion preprint [@Blum2026] reports further benchmarks.

The write cost proved the harder half of the trade-off. Derivation initially ran from Django
`post_save` signals on every OMOP row — simple and always consistent, but derivation time grows
with a patient's history, and for a bulk-loaded patient one row-level write cost 12–32 s. The design now
derives once per ingested bundle or bulk batch rather than once per row, and lets bulk clients defer derivation and request it explicitly, in which case it
runs on a task queue (Celery) and reports success or failure through a status endpoint.
Derivation clears and rebuilds every field, so it is idempotent and a duplicate or retried job
is harmless. The price is a short window in which `PatientRecord` trails the OMOP tables, which
remain the source of truth.

Other components include FHIR-to-OMOP ingestion that maps LOINC, SNOMED CT, and RxNorm codes and
retains the source value when no standard concept is found; line-of-therapy inference that
supplements the ARTEMIS approach [@Golozar2023] with oncology-specific regimen rules; and
OAuth2 / SMART on FHIR authorization [@Mandel2016].

# Research Impact Statement

PRomop runs in production in two separately operated deployments: the HealthTree Foundation
(approximately 14,000 blood-cancer patients) and CancerBot (approximately 3,500 patients), a
service founded and led by the author. Together they support clinical trial matching against
18,000+ trials with structured eligibility criteria across five cancer types (multiple myeloma,
follicular lymphoma, chronic lymphocytic leukemia, breast cancer, and diffuse large B-cell
lymphoma). No data from either deployment appears in this paper or in the repository; every
result reported here uses synthetic patients.

Three open-source systems consume `PatientRecord` rather than deriving patient state
themselves: PRism [@PRism], a population analytics dashboard; EXACT [@EXACT], a clinical trial
matching engine; and a patient-portal application [@PHR], which loads PRomop's patient-record
interface at runtime as a Module Federation remote. Sharing one derivation eliminated a class of bugs in
which applications disagreed about the same patient.

The project has been developed in the open, in a public GitHub repository, since September 2025: roughly 1,900
commits, more than 700 pull requests, four tagged versions (v1.0.0–v1.3.0) with a
changelog, Zenodo-archived releases, and continuous integration running two backend test suites
and the frontend suite on every pull request. Four developers have contributed code (see
*Acknowledgements*), and changes land through reviewed pull requests. With the synthetic generators, archived benchmark cohort, and Docker Compose setup, the software can be installed, exercised, and benchmarked without real
patient data.

# AI Usage Disclosure

Generative AI tools were used extensively throughout this project: Anthropic Claude through
Claude Code (Claude Sonnet 4.6; Claude Opus 4.6, 4.8, and 5; Claude Fable 5), and GitHub Copilot.
AI-assisted commits carry a `Co-Authored-By` trailer naming the model, so the extent of use is
visible in the repository history. AI assisted with code generation, debugging, test authoring,
code review, documentation, and drafting portions of this paper. The architecture and the
clinical derivation rules were decided by the author. All AI-generated changes were reviewed by
a human through pull requests and had to pass the automated test suites. The author accepts full
responsibility for the correctness and scholarly integrity of the software and manuscript.

# Acknowledgements

Thank you to Vladimir Tarasov (Module Federation remote, patient-facing FHIR sync, and frontend
integration), Leonid Morozov (OMOP write paths, security hardening, and webhooks), and Nikita
Shpilevoy (bulk OMOP endpoints, cross-instance data copy, and infrastructure) for their code
contributions. Thank you to HealthTree Foundation for funding and feedback, and to advisors
Steve Labkoff and Yuri Quintana for guidance and insight.

# References
