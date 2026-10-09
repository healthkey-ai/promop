# Therapy source import audit

Read-only comparison on 2026-09-14 of CancerBot production reference tables,
PRomop Render staging, and the user's
`Therapy_Component_Category_Mapping with Concept IDs (1).xlsm` workbook.
The [machine-readable audit](therapy-import-audit.json) records the workbook
SHA-256, source-row evidence, literal differences and normalization rules.
The workbook's first three primary sheets were compared; the later `Copy of`
sheets were not treated as additional catalogs. No workbook macros were run.

## Catalogs and relationships

| Catalog | CancerBot | PRomop | Finding |
|---|---:|---:|---|
| Therapy / regimen | 239 | 244 | All CancerBot codes exist in PRomop; five supplemental regimens |
| Component | 186 | 187 | All CancerBot codes exist; PRomop also has an empty-code record |
| Category / class | 91 | 91 | CancerBot has `growth_factors`; PRomop has an empty-code record instead |
| Therapy–component link | 394 | 391 | Three CancerBot null-component links; ixazomib differs |
| Component–class link | 493 | 490 | Four CancerBot null-category links; PRomop has an investigational-agent blank-class link |
| Therapy–disease–line link | 604 | 681 | Different null-link handling and supplemental supportive coverage |

Every relationship row in the supplied workbook is represented in PRomop:
391 therapy–component, 490 component–class and 600 therapy–disease–line rows.
The committed seed CSV relationship sets match the workbook after explicit
disease-code and round-label normalization. This includes two invalid targets:

- `Therapy  Component Mapping`, row 350: `ixazomib` has an empty component
  code/title. PRomop created component ID 175 with empty code/title and linked it.
  Live CancerBot links this therapy to the named `ixazomib` component instead.
- `Component  Category Mapping`, row 87: `investigational_agent` has an empty
  category code/title. PRomop created class ID 27 with empty code/title and linked it.

These are missing source targets, not legitimate Unknown choices. Correction
must use existing managed relationships and check references before retiring
the empty records. Null CancerBot links must not become invented catalog entries.

All 81 additional PRomop disease/line links and all five additional regimen
codes are accounted for by `0221_seed_treatment_editor_catalogs`. The additional
codes are `bortezomib_maintenance`, `her2_targeted_therapies`, `hormonal_therapy`,
`immunoglobulin_replacement_therapy` and `radiotherapy`. They are subsequent
supportive-therapy additions, not evidence of a failed spreadsheet parse.

## Concept assignments

Thirty workbook rows have numeric IDs but null PRomop assignments: 15 regimens,
six components and nine classes. They refer to 29 distinct IDs. The
[vocabulary query evidence](therapy-workbook-concepts.json) records current
concept existence, namespace, standard status, domain and class.

- Twenty-one distinct IDs, affecting 22 rows, are absent as OMOP concept IDs.
  Two values in the workbook's Concept_ID column are SNOMED source codes:
  `387222003` resolves by code to OMOP 4306720, and `416234007` to OMOP 4166414.
  That is namespace evidence, not approval of either target for a regimen role.
- Eight IDs currently exist but are unassigned. Seven are nonstandard RxNorm
  Brand Name/Precise Ingredient concepts; one is a standard Clinical Drug Form.
  Current existence does not establish availability at the historical import
  time, nor suitability as a regimen/component/class target.
- No numeric mismatch has been converted into an approved mapping. Existing
  assignment totals remain 192 regimens, 156 components and 66 classes.
- CancerBot's three main catalog tables currently have null `omop_concept_id`
  throughout. Spreadsheet mappings and PRomop's managed assignments are separate
  evidence; do not claim those assignments are installed in CancerBot.

The loader resolves supplied IDs only if the concept exists. It also permits
empty codes and does not make the whole load atomic. Its companion
`generate_therapy_csvs` expects the older single-sheet eight-column layout and
skips two headers; it must not be run unchanged against this three-sheet XLSM.
Coordinate correction and import validation with #1230, preserving curator
decisions and the existing catalog management APIs.

## Planned versus administered

CancerBot `PlannedTherapy` has 94 entries and 175 disease/round links. It is a
separate source catalog from the 239 `Therapy` entries. The earlier observation
that only eight planned entries matched PRomop regimens by both code and title
was a comparison across these separate catalogs, not a failure to import Therapy.
The live reference export preserves planned codes and eligibility independently;
it does not create DrugExposure rows or automatically equate labels.
