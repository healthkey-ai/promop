# Disease-specific demo coverage

Issue #1260 audits the three Render staging synthetic cohorts (1,000 records
per organization) and corrects data capture as well as demo population.

## Baseline audit, 13 September 2026

| Cohort | Existing useful coverage | Important gaps |
| --- | --- | --- |
| FL | Stage/ECOG/LDH: 1,000; therapy: 959; transformation history: 1,000 | Grade/GELF/marrow involvement: 0; FLIPI score and factors: 1 |
| MM | Stage/therapy/M-protein/free light chains: 1,000 | Myeloma type/transplant history/eligibility: 0; MRD: 1; displayed kappa/lambda ratio: 0 |
| BC | TNM/receptors/Ki-67/therapy: 1,000 | ECOG/menopause/grade/Oncotype/PD-L1: 0; genomics recorded on 250 |

These are presence counts, not evidence that every existing value is clinically
consistent. Unknown and empty values do not count; false and numeric zero do.
The FL organization contains 88 transformed DLBCL records and one MCL record.
The backfill retains historical FL assessment for transformed patients and
skips the MCL record instead of putting FL-only fields on it.

## Which fields belong where

| Disease | Routine disease and treatment context | Conditional assessments |
| --- | --- | --- |
| FL | Histologic grade including 3A/3B, Ann Arbor stage, FLIPI factors, GELF burden, symptoms, nodal sites, marrow involvement, LDH/Hb, transformation and prior therapy | GELF establishes burden, not FLIPI prognosis. Marrow involvement alone is not a GELF criterion. PD-L1/TNM metastasis fields are absent from the routine FL view. |
| MM | M-protein type and burden, serum free light chains/ratio, marrow plasma cells, ISS/R-ISS, cytogenetics, renal function, calcium/Hb, transplant history/eligibility and therapy outcomes | MRD after a recorded complete response/remission; a transplant date only when a transplant is recorded. Routine PD-L1 and solid-tumor TNM are absent. |
| BC | Histologic grade/type, TNM, ER/PR/HER2, Ki-67, menopause, ECOG, therapy intent and previous therapy | Synthetic Oncotype results are restricted to HR-positive/HER2-negative stage I/II with N0/N1; PD-L1 CPS and 22C3 assay to metastatic TNBC; bone-only metastasis only in metastatic disease. |

FLIPI-1 gives one point each for age **>60**, stage III/IV, Hb <12 g/dL,
more than four nodal areas, and LDH above the laboratory upper normal limit.
0–1 = low, 2 = intermediate, 3–5 = high risk. The UI shows each checkbox's
contribution and the addition producing the total. The server recomputes the
score from those selections; it cannot be independently edited. A null factor
list is unassessed, whereas an explicit empty list is an assessed score of zero.
Historical cancerbot factor tokens are accepted. The criteria are baseline
assessments and do not silently change when unrelated current labs are edited.
Historical numeric FLIPI scores without a factor checklist remain visible and
are preserved on unrelated edits and OMOP extraction. An explicit checklist
replaces the historical score; an explicit clear remains authoritative during
refresh while the old numeric source is still present. Demo backfilling does
not invent missing factors for an already recorded numeric score, and recovery
does not treat an assessed empty FLIPI/GELF checklist as missing.

The GELF checklist makes the selected high-burden criteria explicit. It uses
mass >7 cm, at least three areas each >3 cm, B symptoms, organ compromise,
symptomatic splenomegaly, effusions, circulating lymphoma cells, or
lymphoma-related neutropenia/thrombocytopenia. Any selected criterion yields
`Met`; an assessed empty checklist yields `Not Met`.

Clinical references:

- [NCI: Indolent B-cell lymphoma](https://www.cancer.gov/types/lymphoma/hp/indolent-b-cell-lymphoma-treatment-pdq)
- [Lymphoma Research Foundation: FLIPI factors and risk groups](https://lymphoma.org/wp-content/uploads/2019/10/Follicular-Lymphoma.pdf)
- [GELTAMO 2026 FL clinical guideline](https://www.mdpi.com/2072-6694/18/3/395)
- [GELF burden definitions](https://pmc.ncbi.nlm.nih.gov/articles/PMC9490109/)
- [NCI: Multiple myeloma](https://www.cancer.gov/types/myeloma/hp/myeloma-treatment-pdq)
- [NCI: Breast cancer biomarkers](https://www.cancer.gov/types/breast/diagnosis/breast-cancer-biomarker-tests)
- [ASCO: Metastatic breast cancer biomarker guidance](https://ascopubs.org/doi/pdfdirect/10.1200/JCO.22.01063?role=tab)

## Repair and generation

```bash
python manage.py backfill_sample_disease_profiles
python manage.py backfill_sample_disease_profiles --confirm
```

Default scope is the known sample organizations. `--org-slugs` selects a subset;
`--limit` supports smoke tests. A custom generated organization additionally
requires explicit `--disease FL|MM|BC`. `--dry-run` always prevents writes.

The command recovers legacy generated facts first, then fills missing relevant
fields. It preserves pending user edits. Recovered stage facts supersede earlier
synthetic stage fallbacks. Assessments are internally consistent synthetic
examples, not inferred clinical recommendations or prevalence estimates.

Batched writes avoid one remote database transaction per field. Approved scalar
mappings use their OMOP table/concept; unmapped facts use concept 0 and an
explicit `demo:` source identity. Provenance distinguishes recovered sample
facts from newly synthetic values. No full OMOP refresh runs during backfill.
Import-time derivation can recover the local assessments later.

The FL, MM, and BC generators now emit missing source assessments. Local
assessment codes use the HealthKey demo code-system URI, not fabricated LOINC
identifiers. The FL generator previously used `21912-1` for a nodal count, but
[LOINC identifies it as a categorical regional-node staging field](https://loinc.org/21912-1).
Those old keys are read only by the explicit sample repair path. Each disease's
enrichment pass also completes its selected cohort, so the combined generation,
import, and enrichment commands retain coverage on future runs.
Generated profile observations stay beside their own Patient entry so that
single-patient command-line import batches remain self-contained. Breast-cancer
completion reads nodal stage (`21906-3`) before evaluating its Oncotype subgroup.

The grade migration preserves numeric grades as text and can store 3A/3B.
All factor inputs remain editable through PatientRecord. Computed outputs do
not trigger OMOP-to-PatientRecord refresh on a UI save.
