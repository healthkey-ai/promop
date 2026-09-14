# Field and field-value concept mapping enhancements

Plan for [#26](https://github.com/healthkey-ai/promop/issues/26) and
[#21](https://github.com/healthkey-ai/promop/issues/21). Prepared 2026-09-13
against PRomop `a5e42c5`, the local CancerBot option definitions, and read-only
queries of staging reference/vocabulary tables. This document specifies the
implementation work; it does not approve mappings or change staging data.

## Current checkpoint and next action — 2026-09-14

Implementation checkpoint: [PR #1271](https://github.com/healthkey-ai/promop/pull/1271),
based on `dev` through `144cd98`, delivers the first reproducible inventory slice.
The checked-in inventory was refreshed read-only on **2026-09-14**. Section 2's
original baseline counts and candidate tables remain the **2026-09-13 snapshot**;
use the linked inventory report for the later reference export. No clinical
mapping was approved and no staging reference or patient data was changed.

All nine implementation issues (#1223–#1231) remain open. The current schema
still keys `FieldChoice` by field name and display text; `FieldChoiceCode` has
vocabulary/code and a primary flag, without the proposed scoped review model.
The generic scalar writer clears `value_as_concept_id`, and generic curated
readback selects number/text. The shared answer-mapping work is still required.

**Next: complete [#1223](https://github.com/healthkey-ai/promop/issues/1223), the
field-and-value inventory, before starting the dependent schema change #1224.**
The inventory acceptance requirements below remain the guide for completing
#1223; the first generator/report slice is implemented, not the entire issue:

1. Define a versioned manifest format using the row requirements in section 3.
   Keep fields and values distinguishable, with stable source keys, typed values,
   scope, question/answer roles, candidate evidence and explicit dispositions.
2. Export the PRomop reference lists, mappings, choice codes and therapy catalogs;
   collect CancerBot's static and database-generated options. Record source
   revisions, export dates, vocabulary release and missing source coverage.
3. Reconcile those sources with descriptors, recipes and frontend options. Report
   duplicate identities, context conflicts, missing mappings and coverage totals.
   Give every discovered row a disposition; preserve unresolved and no-equivalent
   cases rather than dropping them from the counts.
4. Investigate the captured SNOMED provenance inconsistency and revalidate
   vocabulary/code, dates and domains before treating any candidate as reviewed.
   Commit the generator, safe reference artifacts and a repeatable coverage report.

**First inventory slice implemented on 2026-09-14:** the
[reproducible reference inventory](docs/field-mapping-inventory/README.md) now
exports staging reference tables, typed source rows, frontend constants/control
bindings, the frozen Genomics catalog, CancerBot static fragments, exact
candidate evidence and both vocabulary release mechanisms. The
[coverage report](docs/field-mapping-inventory/coverage.md) accounts for 2,801
source occurrences, including 414 fields, without dropping unresolved rows.
The SNOMED provenance investigation traced the metadata string to a historical
benchmark helper; this does not certify individual staging concepts.

The user confirmed that staging's therapies, components, classes and regimen/
disease mappings were generated from CancerBot. They are the authoritative
therapy inventory source: 34 public lists are covered by the staging catalogs,
25 lists by complete static source, and 20 planned lists have catalog coverage
with picker context pending. No duplicate therapy export is required.

**#1223 remains open.** Of 172 public option-list bindings, 93 need further
source/provider reconciliation; database-driven lists in that remainder need
live reference coverage. No live CancerBot database connection was configured.
Dynamic provider reconciliation, retired-option history, destination/context
crosswalks and semantic review also remain. Continue those inventory tasks;
do not treat this first slice as completing the prerequisite for #1224.

### Handoff: continue on another machine

**Completed in PR #1271:** versioned reference manifest and coverage report;
typed field/value identities; frontend and CancerBot source extraction; staging
therapy catalog/disease-round coverage; exact code/name/synonym/Maps-to evidence;
SNOMED provenance investigation; and regression coverage for the inventory.
The merge-review fixes use psycopg SQL composition, keep dynamically transformed
literal lists unresolved, and update live-import bindings/provider counts
consistently (including empty lists and planned-list coverage).

The focused suite has **22 passing tests**. The refreshed staging export exercises
the read-only SQL path; CI and merge status are recorded on PR #1271. The final
merge commit is discoverable from that PR, so this checkpoint does not depend on
a machine-local worktree or temporary runner.

After PR #1271 is merged, start a new branch from current `origin/dev` in a clean
checkout. For example, from an existing clone:

```sh
git fetch origin
git worktree add ../promop-field-inventory-next -b feat/1223-next origin/dev
```

Read [inventory instructions](docs/field-mapping-inventory/README.md),
[coverage](docs/field-mapping-inventory/coverage.md), and
[manifest](docs/field-mapping-inventory/manifest.json). The manifest contains the
source revisions/file hashes, reference rows, candidate evidence, per-list
coverage states, validation flags and implementation owners. It can be reviewed
without access to either live database. To refresh it, configure the intended
reference database locally and follow the portable command in the inventory
instructions; never copy credentials into the repository. Staging remains Render.

**Next work, in order:**

1. Reconcile the 93 outstanding CancerBot public lists: establish which are
   static/provider-resolvable, which are trial/admin exclusions, and which need
   live reference coverage. Use `cancerbot_bindings` and
   `totals.source_coverage.cancerbot_public_lists` in the manifest. Do not request
   another therapy catalog export: staging already supplies that CancerBot data.
2. Resolve the 20 planned-picker contexts, remaining dynamic frontend providers,
   and nested genetic relationships. Preserve planned versus administered
   status, staging-system/basis, marker polarity and typed unknown values.
3. Finish destination/context crosswalks, aliases and retirement/replacement
   history. Review the manifest's domain/table and read-recipe conflicts and
   expand semantic candidate evidence where exact searches are insufficient.
   Coordinate vocabulary lineage repair with #461/#623 before bulk approval.
4. Reconcile complete source/disposition totals and representation decisions;
   only then close #1223 and start #1224. #1225–#1231 remain subsequent work,
   with dependencies unchanged in section 9. Parent #21 and #26 remain open.

The inventory can begin with available reference sources, but #1223 is complete
only when its full source coverage, totals and representation decisions are
accounted for. Then deliver stable scoped choices and reviewed answer mappings
in #1224; #1225 (curation UI/API) and #1226 (projection/readback) build on that.

Genomics has advanced since the original snapshot: use the
[implemented Genomics architecture](docs/genomics_architecture.md) and
[remaining Genomics work](docs/genomics_implementation.md) for current ownership.
Issue #1229 adds contextual value mapping to that finding/component model;
it must preserve the frozen catalog, structured findings and reviewed recipes.
The [current field mapping reference](field_to_concept_mapping.md) supplies the
UI-to-PatientRecord-to-OMOP baseline, but is not the exhaustive answer inventory.

## 1. Intended result

Every in-scope field and every enumerated value must have an explicit disposition:
an existing verified mapping, an additional/corrected standard Athena mapping,
a structured or computed representation, or a documented reason it cannot map.
Having a concept for the field does not establish a mapping for its answers.

Extend the existing field-mapping and choice capabilities. Retain the existing
therapy, component, and class reference tables and their relationships. Do not
implement the original #26 proposal to recreate 25 lookup models and convert
every clinical text column to a foreign key: those lookup models largely exist,
and many fields are projections, arrays, numbers, or contextual assessments.

For example, an ER result needs both its measurement concept and a positive or
negative answer concept. `tumor_stage=T1a` needs a T-stage question, the T1a
answer, and its staging system/basis. A therapy choice resolves through the
therapy reference model. An unknown or unrepresentable value remains usable
without being assigned an unrelated standard concept.

## 2. Verified baseline and limitations

| Staging reference data | Observed coverage |
|---|---|
| `field_concept_mapping` | 314 rows; 217 approved |
| Field mappings passing the initial external-standard screen | 191 of 314, across all statuses; this is not a count of semantically correct or approved mappings |
| `field_choice` | 262 choices across 47 fields |
| Choices with at least one `field_choice_code` | 58; the other 204 have no code |
| `field_choice_code` | 66 rows; 54 resolve by vocabulary/code; 46 pass the initial external-standard screen |
| `therapy_regimen` | 244 rows, 192 with a concept FK |
| `therapy_component` | 187 rows, 156 with a concept FK |
| `therapy_class` | 91 rows, 66 with a concept FK |

The initial screen used `standard_concept='S'`, null `invalid_reason`, ID below
2,000,000,000, and source other than `HealthKey`. Full validation must also check
validity dates, external vocabulary provenance, domain, and clinical meaning.
Therapy FK counts do not establish that every attached concept passes these checks.

At the 2026-09-13 snapshot, the latest published `vocabulary_release` row was ID 11, Athena version
`v5.0 29-AUG-26`, published 2026-08-31. The separate `vocab_release` table returned
no rows. Report which release mechanism is used, rather than conflating the two.
Loaded vocabulary metadata includes LOINC 2.80, RxNorm 20260105, HemOnc 2024-12-19,
ICDO3 SEER 06/2020, NCIt 20220509, and OMOP Genomic 20240216. The queried CTCAE
vocabulary row was absent. SNOMED's vocabulary-version string says
`SNOMED CT (synthetic, benchmark seed)` even though externally numbered concepts
are present. That provenance inconsistency requires investigation before bulk
approval; a standard flag alone cannot certify an Athena mapping.

Only vocabulary and reference metadata were queried. The inventory is not an
audit of patient values or a completed clinical curation of every choice.
The candidate tables below record actual staging results; a candidate is not
automatically a clinically approved mapping. A failed name search does not prove
that no equivalent exists in Athena.

### Existing implementation to extend

| Component | Current behavior and required extension |
|---|---|
| `FieldConceptMapping` in `omop_core/models.py` | One row per field, with question concept, destination, source key, value kind, vocabulary and review status. Preserve and repair existing rows; add missing rows. |
| `FieldChoice` / `FieldChoiceCode` | Choices use display text as identity; codes are vocabulary/code strings with a primary flag. Add stable value identity, context, resolved target and review state. |
| `FieldChoiceEditor.tsx`, `/api/v1/field-choices/` | Choice CRUD and manual code entry already exist. Add concept search, explicit target role and mapping review. |
| `write_descriptor.py`, `omop_projection.py` | Current UI saves PatientRecord first and projects approved mappings on the backend. `project_single_value()` clears `value_as_concept_id` and writes scalar number/text; extend this path to resolve answers. |
| `curated_values_from_snapshot()` / `patient_record_service.py` | Generic curated readback currently reads number/text, and several disease extractors ignore coded answers. Both need the same reverse value resolution. |
| `field_curation_transfer.py` | Transfers choices/codes when selected, but defaults to mappings and synonyms. Add new metadata and an explicit complete field-plus-values transfer operation. |
| `mapping/therapy.py`, therapy reference loaders/APIs | Existing regimen/component/class resolution, relationship expansion and quarantine handling remain authoritative for therapies. |
| `suggest_field_concept_mappings` | Field-only lexical suggestion workflow; its `kind == 'unmapped'` selection must be reconciled with current PatientRecord-first descriptors. Add separate field/answer coverage and suggestion modes. |

The current write architecture is documented in
[patient-record-first-writes.md](docs/patient-record-first-writes.md). Preserve
its pending-edit protection, partial projection failure handling, clear markers,
same-day write locking and external refresh behavior. Some older source comments,
issues and `clinicalFacts.ts` describe the previous OMOP-first UI; they must not
drive this implementation.

## 3. Complete gap inventory required by #26

Build a checked-in manifest with one row per field and one per actual option,
including options supplied by lookup tables and nested genetic catalogs, not just
the 262 `FieldChoice` rows. Compare CancerBot `trials/services/value_options.py`,
its seed/migration data, PRomop lookup rows, `FieldChoice`, frontend constants,
descriptors and mapping recipes. Some CancerBot options are database-generated;
source code alone cannot establish the complete live catalog. Export those
reference lists when doing the implementation inventory.

Each row must record source option-list/key/label, destination field or nested
path, canonical typed value, aliases, disease, system/edition/method context,
existing field and choice mappings, candidate vocabulary/code/ID/name/domain,
mapping role, search evidence, release, disposition, reviewer and owning issue.
Include retired options and their replacement aliases. Totals must reconcile:
no field or option may disappear because it has no candidate.

| Scope from #26 | Representation and work required |
|---|---|
| MM `stagesMm`, `r_iss_stage`, progression | Separate ISS, R-ISS and other staging systems; investigate the legacy I–IV list before equating it with R-ISS. Preserve active/smoldering meanings rather than equating all progression labels. |
| MM `boneLesions` | CancerBot has unknown/1/2/more-than-2; staging choices currently have Yes/No/Unknown. A presence flag cannot represent count. Preserve counts and the `>2` comparison, or structured lesion facts, with a compatible legacy projection. |
| MM transplant history and eligibility | Reuse `StemCellTransplant` and `SctEligibility`; distinguish completed procedures, eligibility, pre/post status, relapse-after-transplant and tandem history. No procedure occurrence for an eligibility assertion. |
| Treatment outcomes | Enumerate every option, including CR/sCR/VGPR/PR/MRD/SD/PD, by disease and response system. MRD and response categories may need separate observations. Reuse `TherapyOutcome` and the work in #253. |
| FL stages and tumor grades | Scope Ann Arbor/Lugano stage and grades 1/2/3/3A/3B; determine whether unsuffixed grade 3 remains a valid legacy option. Do not equate FL grade with breast Nottingham grade. |
| FL FLIPI | Preserve five criterion inputs (age, stage, hemoglobin, nodal areas, LDH), score and risk category as different values/facts. Do not map a numeric criterion threshold to a categorical answer by string resemblance. |
| FL GELF | CancerBot exposes seven selected criteria; staging only has Met/Not Met/Unknown. Capture the seven criteria and a separately derived aggregate. Do not silently reduce the list to one Boolean. |
| Cytogenetic and molecular markers | Reconcile marker/category/connection models and positive/negative marker assertions; support multiple independently coded results. Keep `none`, unknown and not-tested distinct. |
| BC histology, biopsy grade, TNM and staging basis | Curate every histologic type, grade 1–3, all T/N/M options, and c/p/yp contexts. Share mapping work with #21 as detailed below. |
| BC ER/PR/HER2/AR, HR and HRD | Map result values independently of question concepts; retain equivocal/unknown and assay context. HR low/high-expression labels require thresholds and evidence, not inference from positive alone. |
| BC PD-L1 assay | Enumerate SP142, 22C3, SP263, 28-8 and Other. Clone-specific measurement concepts are possible question concepts, not interchangeable assay answer values. Keep assay, TPS, CPS and immune-cell percentage distinct. |
| BC menopausal status | Map pre/post and retained perimenopausal/unknown options explicitly; reconcile CancerBot codes and PRomop display labels. |
| Genetic genes/variants/origins/interpretations | Inventory every dependent option and gene/variant relationship. Resolve exact variants with gene, transcript/reference assembly and method context where applicable; retain unmapped source identifiers. Use the current Genomics finding/component architecture and structured `genetic_mutations`; preserve the frozen catalog and reviewed recipes. |
| CLL stages/Binet | Clarify generic `stagesCll` versus Rai 0–IV and Binet A/B/C; use contextual identities and separate question concepts. |
| CLL protein expressions | Each marker and its polarity form an assertion; `CD5+` is not merely the generic positive answer with marker identity discarded. |
| CLL Richter transformation | Distinguish transformation type, present/absent/unknown and transformation date; reuse existing models and linked events. |
| CLL tumor burden/morphologic variant/disease activity | Verify actual field presence (including `morphologic_variant`), option identity and criteria. Indolent/watch-and-wait and active disease requiring treatment are contextual clinical meanings. |
| Therapy options in every disease and treatment round | Use regimen/component/class tables and disease/round links for first, second, later, supportive, planned and concomitant therapy. Audit missing mappings and picker coverage; preserve planned versus administered status. |
| Pre-existing condition categories | Reuse and populate category relationships; a broad eligibility category may require a concept set. Keep selected categories separate from individual diagnosis events. |
| Neuropathy/toxicity and positive/negative | Include grade 0 as well as 1–4 and unknown where the source supports them. Grade is numeric or instrument-specific; unknown is never false/grade 0. |
| Other lookup models named by #26 | Account for ethnicity, language, language skill and preferred country. Use Person/Location or administrative references where appropriate. Coordinate #1146; hidden language fields need compatibility handling, not new UI. |
| MCL | Reuse the value-mapping mechanism and link #468/#542/#1149 for MCL field parity; do not drop MCL therapy or marker values from the shared inventory. |

Existing choice fields outside the original issue (for example smoking, alcohol,
diet, education and social support) must participate in migration compatibility
and coverage reporting. Their broader clinical remapping belongs with
#1075/#1077, avoiding duplicate work.

## 4. Athena curation and mapping rules

1. Resolve exact vocabulary/code pairs first. Search names and
   `concept_synonym` for candidates, then examine valid `Maps to` relationships
   for non-standard source concepts. Keep original source codes separately.
2. A target must be current, standard, externally sourced, and appropriate to
   the destination role. Validate both relationship and target validity. Record
   vocabulary release identity and the rationale for selection.
3. Inspect ambiguous and multi-target relationships; never select the first
   result merely to fill a gap. Expand legitimately composite meanings through
   an explicit projection recipe with event links.
4. Distinguish a question concept, a coded answer, a whole clinical assertion,
   and a contextual modifier. A Meas Value concept cannot serve as the generic
   field question; a Type Concept such as EHR is provenance, not clinical meaning.
5. Prefer existing standard equivalents. If none has been established, record
   `needs_review`, `ambiguous`, `no_equivalent`, `not_applicable`, or
   `requires_structured_representation`. `no_equivalent` requires documented
   searches and review; it is not the default result of a short name query.
6. Keep unknown, none, not assessed, equivocal, other and not applicable distinct.
   An explicitly selected Unknown may map to a valid answer concept; an absent
   field or clear operation must not become an assertion of Unknown.

The separation of question and answer columns follows the
[OMOP CDM 5.4 Measurement and Observation definitions](https://ohdsi.github.io/CommonDataModel/cdm54.html#measurement).
Domain-specific occurrence and contextual mappings require their own write recipe.

### Verified question candidates and corrections for #21

All candidates in this table were present as standard concepts in staging.
Existing approvals still need domain and semantic validation.

| Field/context | Candidate ID; vocabulary:code | Required disposition |
|---|---|---|
| Clinical T | 3008841; LOINC:21905-5 | Replace the generic staging question for `tumor_stage` in clinical context. |
| Clinical N | 3007727; LOINC:21906-3 | Reuse existing question; complete the single-table Measurement recipe. |
| Clinical M | 3006575; LOINC:21907-1 | Add clinical-M recipe; do not treat pathological 21901-4 as clinical M. |
| Pathological T/N/M | 3016308 / 3013189 / 3018082; LOINC:21899-0 / 21900-6 / 21901-4 | Add distinct read/write recipes with basis and tumor linkage. |
| Clinical/pathological overall stage | 3022698 / 3008495; LOINC:21908-9 / 21902-2 | Retain system, edition and basis; no generic stage mixing across diseases. |
| Histologic type | 40762908; LOINC:59847-4 | Existing question; add histology/behavior answers or an explicit occurrence representation. |
| Nottingham grade | 3047285; LOINC:44648-4 | Existing question; preserve numeric grade and map categorical grade answers explicitly. |
| Ki-67 percentage | 3018217; LOINC:29593-1 | Existing curated concept is appropriate to investigate; replace conflicting 85319-2 recipes/extractor assumptions. |
| Tumor size | 3018102; LOINC:21889-1 | Correct Observation destination to Measurement; preserve units and measurement/lesion context. |
| HRD test | 1469934; LOINC:107286-7 | Correct destination to Measurement; read the test result rather than compute from HR. |
| AR result | 3032783; LOINC:49457-5 | Reuse question and add positive/negative/equivocal/unknown answers. |
| Test methodology | 42527891; LOINC:85069-3 | Complete the existing proposal and event link; remove 85337-4 as methodology. |
| Test specimen | 3015746; LOINC:31208-2 | Existing recipe; link specimen to the same test event. |
| Oncotype invasive/DCIS score | 35918544 / 35918760; NAACCR:3904 / 3903 | Candidate numeric-score questions; select by test indication. Existing 35933430 is an assay answer, not a score question. |
| Menopausal status | 4172857; SNOMED:276477006 | Existing Observation question; resolve answers with context. |
| HR status | 44791967; SNOMED:310871000000100 | Existing Observation question for an explicit result; keep calculated HR provenance distinct. |

### Verified answer candidates

These are reusable only with the appropriate field and context. Store both ID
and vocabulary/code in reviewed seed data and re-resolve against each deployment.

| Answer | Standard candidate ID | Vocabulary:code |
|---|---|---|
| Positive | 9191 | SNOMED:10828004 |
| Negative | 9189 | SNOMED:260385009 |
| Equivocal | 4172976 | SNOMED:42425007 |
| Explicit unknown answer | 45877986 | LOINC:LA4489-6 |
| Nottingham grade 1 / 2 / 3 | 36210095 / 36210546 / 36210757 | LOINC:LA27823-6 / LA27824-4 / LA27825-1 |
| Stage I / II / III / IV | 45879260 / 45883600 / 45883601 / 45879261 | LOINC:LA15457-7 / LA15458-5 / LA15459-3 / LA15460-1 |
| TX / T0 / Tis | 45880974 / 45878380 / 45878381 | LOINC:LA3601-7 / LA3638-9 / LA3608-2 |
| T1 / T1mi / T1a / T1b / T1c | 45878379 / 46237476 / 45876312 / 45880104 / 45881600 | LOINC:LA3637-1 / LA21854-7 / LA3636-3 / LA3633-0 / LA3630-6 |
| T2 / T3 / T4 | 45884279 / 45876313 / 45882493 | LOINC:LA3628-0 / LA3624-9 / LA3620-7 |
| T4a / T4b / T4c / T4d | 45881604 / 45880976 / 45881602 / 45881601 | LOINC:LA3619-9 / LA3618-1 / LA3617-3 / LA3616-5 |
| NX / N0 / N1 / N1mi | 45884289 / 45881617 / 45881613 / 46237472 | LOINC:LA4745-1 / LA4368-2 / LA4537-2 / LA21822-4 |
| N1a / N1b / N1c | 45880982 / 45881615 / 46237062 | LOINC:LA4264-3 / LA4535-6 / LA21866-1 |
| N2 / N2a / N2b | 45881614 / 45882498 / 45880111 | LOINC:LA4534-9 / LA4533-1 / LA4517-4 |
| N3 / N3a / N3b / N3c | 45882499 / 45884288 / 45878649 / 45881616 | LOINC:LA4545-5 / LA4529-9 / LA4528-1 / LA4527-3 |
| M0 / M1 | 45878650 / 45876322 | LOINC:LA4629-7 / LA4628-9 |
| M0I+ (candidate for M0(i+)) | 46237080 | LOINC:LA21876-0 |
| Clinical staging | 4126032 | SNOMED:260998006 |
| Pathological staging | 4128939 | SNOMED:261023001 |
| Germline / somatic | 45880308 / 45880309 | LOINC:LA6683-2 / LA6684-0 |
| Pathogenic / likely pathogenic | 45884094 / 21498358 | LOINC:LA6668-3 / LA26332-9 |
| Complete response | 36309727 | LOINC:LA28366-5 |

All 15 seeded T choices and 14 N choices have lexical standard-answer candidates
above, but matching the prefix alone does not validate the explanatory label,
staging edition or clinical/pathological context. A broader search found M0I+
as a candidate for M0(i+); review the definition before approving that alias.
Post-neoadjuvant pathological staging remains a separate context to resolve.

Other useful staging results:

- Ann Arbor non-Hodgkin staging question: 4110411, SNOMED:254374001,
  Measurement. FLIPI question: 35917496, NAACCR:lymphoma@2910, Measurement.
- ISS/R-ISS questions: 607125 / 607126, SNOMED:1149162008 / 1149163003,
  Measurement. Binet/Rai questions: 607090 / 607102,
  SNOMED:1149099005 / 1149131009, Measurement. Binet answer curation remains
  outstanding. Rai stage-specific Cancer Modifier concepts returned by search
  are Measurement concepts, not generic answer values.
- FL grades 1/2/3/3A/3B have Condition candidates 44808015 / 44814156 /
  44808028 / 606919 / 606914 (SNOMED:847481000000109 / 847631000000107 /
  847651000000100 / 1148851002 / 1148845007). Choose occurrence versus answer
  representation explicitly rather than placing every grade concept in a value column.
- CD5 question: 3006739, LOINC:29603-8, Measurement; pair with positive/negative
  answers while retaining the marker. Autologous peripheral blood stem-cell
  transplant: 4144157, SNOMED:425983008, Procedure; applicable to the actual
  procedure, not every transplant-history or eligibility option.
- Smoldering myeloma: 4184985, SNOMED:413587002, **Condition**. It is a candidate
  for a diagnosis/assertion representation, not an unconditional Meas Value.
- Pre/postmenopausal states: 4331463 / 4295261, SNOMED:22636003 / 76498008,
  **Observation**. Their placement needs a reviewed Observation recipe.
- PD-L1 clone-specific questions include 42529558 (LOINC:83052-1, 22C3),
  42529176 (83057-0, SP142), 1988568 (99064-8, SP263 document), and
  42529561 (83055-4, 28-8). These are Measurement concepts; a document/report
  concept is not a numeric score and does not automatically supply an assay answer.
- Short standard-name searches found no exact VGPR or stringent-complete-response
  candidate. Continue synonym/source-relationship and response-system searches;
  preserve these distinctions if no standard equivalent is established.

## 5. Extend the field mapper to map values

### Data model

Use the existing `FieldChoice` as the allowed-value record. Add a stable `code`
and typed canonical storage value; keep `display` editable and keep existing
display strings as aliases during migration. Add explicit disease/context scope
(including staging system, edition and basis when needed), with an unambiguous
natural key such as `(field_name, context_key, code)`. Shared answer concepts may
be reused by different scoped choices without merging their clinical meanings.

Add `FieldValueConceptMapping` linked to `FieldChoice`, within the existing field
mapping component. Recommended contents:

- Nullable protected `target_concept` FK, resolved vocabulary/code, mapping role,
  proposed/approved/rejected review state, mapping outcome, rationale, reviewer,
  timestamps, vocabulary release and revision.
- Mapping role distinguishes `value_as_concept`, an occurrence/main concept,
  and a context-dependent recipe. A question override, when necessary, is a
  separately validated part of that recipe; it is not overloaded into the answer FK.
- At most one active approved resolution per scoped choice/role. Keep previous
  decisions in revision history. A reviewed `no_equivalent` can have no target;
  an approved mapped resolution requires a valid target and complete semantics.

Keep `FieldChoiceCode` for source codings and interoperability aliases. Migrate
its primary codes into **proposals**, resolving exact vocabulary/code and
recording unresolved, invalid or non-standard sources. Existing `is_primary=True`
does not imply review approval, nor does it guarantee uniqueness. Enforce
source-code/alias and active-target uniqueness transactionally.

Connect choices to existing field mappings by canonical field identity. Resolve
missing parent mappings without requiring a fake question concept. Computed,
Person-backed and therapy-backed fields can expose controlled values without
pretending to be scalar Measurement/Observation questions.

Keep one canonical `FieldConceptMapping` row per field. For TNM and other fields
requiring multiple question concepts, add explicit scoped recipes under that
mapping (or a narrowly defined staging adapter), retaining the current row as
the default. Reject ambiguous context and source-key collisions; do not encode
multiple write destinations as a comma-separated table name.

### Curation API and UI

Extend the existing choice endpoints/editor and field descriptors to expose the
stable value, aliases, scope, candidate/approved concept, role, outcome and
review metadata. Use the existing Athena search/lookup API with role/domain and
standard filters. Curators can create allowed values with or without a concept,
propose a mapping, review it, retire a value and inspect remaining gaps.

Show question mapping and value mapping coverage separately. A field can be
fully mapped while its answers are unmapped. Distinguish an intentional local
answer from a missing vocabulary, stale target or failed projection. Derive
curation permissions from existing mapping-admin authorization and test them.

Support shared lookup-backed option providers so the editor does not create a
second mutable therapy catalog or a conflicting copy of every legacy lookup.
Keep old API shapes working during a versioned migration to stable value codes.

### Projection and readback

Resolve allowed values and aliases on the server, not by trusting a client-sent
concept ID. A valid choice remains saveable when it lacks a standard equivalent;
strict controlled-value validation must distinguish this from an invalid choice.
Legacy free text is preserved with an explicit migration/unmatched outcome.

For Measurement/Observation mappings, retain the field question concept and
write an approved answer to `value_as_concept_id`, preserving the original
source answer separately. Numbers remain numeric; a coded grade may also retain
its numeric representation when the recipe defines both. Never infer a target
from a display-name substring during a clinical write.

If a valid answer has no mapped concept, store source text with a null answer
concept when a reviewed question recipe supports that representation. If the
question or representation is unresolved, retain the pending PatientRecord
edit and expose the projection gap. Do not fabricate a standard concept or use
concept 0 as a positive clinical finding.

The reverse resolver must recover the canonical field value from the scoped
question/answer pair, then accept legacy number/text aliases. Use it for generic
curated readback and disease-specific extractors; a concept's human-readable
name is not necessarily the value clients expect. Resolve conflicts explicitly.

Multiple answers require one linked fact per selected assertion or a defined
structured representation. Comma-joining a list and assigning one answer concept
loses information. Preserve identifiers and event linkage for therapy, markers,
mutations and multi-criterion assessments.

Clearing a value clears its answer concept and preserves clear-marker semantics.
Mapping changes invalidate descriptors and caches. Reproject pending values
through the existing save path; previously projected facts require a separately
scoped reconciliation job, not an unbounded rewrite on mapping approval.

Apply the same mapping semantics to FHIR `valueCodeableConcept` import/export
and coded OMOP imports while preserving source codings and the governed
source-code resolver. Field-specific aliases must not become globally valid
source-code mappings with different meanings in other fields.

## 6. Breast cancer ETL repair: all of #21

The issue's original premise is outdated: `_get_staging_data`,
`_get_biomarker_data`, `_get_genomics_pathology_data`, `_get_bc_clinical_data`
and computed HR logic already cover parts of this work. Extend and correct them;
creating a method called `get_breast_data()` alone would not fix the issue.

| #21 fields | Required implementation and verification |
|---|---|
| `tumor_stage`, `nodes_stage`, `distant_metastasis_stage` | Read approved question/answer pairs from both supported Measurement and legacy Observation sources, for clinical and pathological T/N/M. Choose the latest applicable fact by clinical date with deterministic tie-breaking, retaining tumor, system, edition and basis. Do not always prefer a Measurement over a newer Observation. |
| `staging_modalities` | CancerBot/staging choices c/p/yp describe staging basis. Preserve this meaning; distinguish post-neoadjuvant pathological assessment. CT/MRI/PET are linked imaging procedures, not values of this enum. Current code can collect stage text as a modality through broad name matching; replace it. |
| `metastatic_status`, `metastasis_status` | Define one canonical extent assessment and compatible aliases, linked to the same tumor/event. Incorporate explicit distant-metastasis conditions and M-stage evidence with a documented conflict policy. Unknown/MX/absent data must remain unknown; the current `'M1' in text` Boolean conversion makes other text false. |
| `lymph_node_status` | Correct the staging mapping that points at LOINC:92837-4 (perineural invasion). Define nodal positivity from appropriate N-stage or explicit nodal involvement facts; preserve unknown and contradictory assessments. |
| `bone_only_metastasis_status` | Current mapped LA4202-3 (36309629) is a Meas Value meaning distant recurrence in bone only, not a question for every metastasis assessment. Require explicit bone-only evidence or a complete scoped assessment; bone metastasis alone does not establish absence of other metastatic sites. |
| `tumor_size` | Measurement with explicit units and lesion/site/date linkage. Normalize compatible units for the consumer without dropping source units or assigning another lesion's size. |
| `histologic_type`, `biopsy_grade` | Handle coded and legacy text/numeric results. Preserve breast histology/site/behavior context and grade versus score distinctions. Keep deprecated biopsy fields as aliases if still needed. |
| `menopausal_status` | Use explicit reviewed concepts and answer aliases, replacing broad name matches that can confuse other menopause-related observations. |
| `ki67_proliferation_index` | Correct the hardcoded 85319-2 usage: staging and the [LOINC definition](https://loinc.org/85319-2/) identify it as HER2. Use the reviewed Ki-67 percentage concept/units and ensure a HER2 result cannot populate Ki-67. |
| `hr_status` | Keep the existing ER/PR-derived behavior but make unknown/equivocal, conflicting tests, dates and explicit versus derived provenance explicit. Low/high expression cannot be calculated from positive/negative alone. |
| `hrd_status` | An assay result, not a value calculable from HR status. Read both coded and textual results of the reviewed HRD measurement; do not assume breast phenotype establishes HRD. |
| `ecog_assessment_date`, `test_date` | Take the date from the corresponding selected ECOG/test event. Do not create an independent generic date assertion or pair the latest result with an unrelated latest date. |
| `test_methodology`, `test_specimen_type` | Link metadata to the selected test/report/specimen. Remove the extractor coupling of methodology and Oncotype to 85337-4, which staging identifies as an ER assay. |
| `oncotype_dx_score` | Use a numeric score question, distinguishing invasive/DCIS tests. A concept naming the assay itself is not a score. Remove numeric ER results as an Oncotype fallback. |
| `androgen_receptor_status` | Reuse the existing question, add answer mapping and end-to-end readback. |

Protect #21's already-populated ER/PR/HER2/TNBC/PD-L1/RECIST fields with regression
coverage. The staging checks also show that 83055-4 is a 28-8 presence test and
83054-7 is a 22C3 interpretation narrative; existing code treats them as numeric
PD-L1 immune-cell percentage/CPS. Audit those neighboring recipes together,
keeping clone, interpretation and numeric score distinct.

Required BC integration scenarios include: codes without display text; legacy
text aliases; cT versus pT/ypT; a newer Observation versus an older Measurement;
different tumors; unknown/equivocal and explicit clearing; Ki-67/HER2 and
ER/method/Oncotype non-interference; score versus grade; correct unit conversion;
linked metadata dates; protected pending user edits; and idempotent refresh.
Use actual verified concept codes/domains in fixtures so tests cannot manufacture
a concept with the wrong clinical name and thereby validate a wrong recipe.

## 7. Therapy integration

The therapy architecture is already documented in
[therapy-reference-tables-architecture.md](docs/therapy-reference-tables-architecture.md).
Audit the 52 unmapped regimens, 31 components and 25 classes and validate the
mapped rows. Retain `TherapyRegimenComponent`, `TherapyComponentClassLink`,
`DiseaseTherapyRegimen` and treatment-round scope. HemOnc regimen, RxNorm drug
and class concepts serve different roles; a classification-only concept must
not be reported as a standard drug fact.

Expose these reference values through the common mapping/coverage interface
using a provider adapter. Reuse existing clinical therapy-line and supportive
therapy writers and quarantine behavior. Coded outcome/intent/discontinuation
values attach to their therapy episode and date; planned therapies must not
become administered drug exposures. Coordinate #252/#253/#1072/#230 and the
existing loader/API tickets rather than porting the catalogs again.

## 8. Delivery, rollout and acceptance

Implement in this order:

1. Freeze the field/value inventory, vocabulary provenance findings and explicit
   representation decisions. Prepare reviewed data files with candidate evidence.
2. Add stable choice identity, scoped value mappings, validation and migration of
   existing codes into proposals. Extend transfer/versioning alongside the schema.
3. Extend curation API/editor and descriptors; make field and answer coverage visible.
4. Add common backend answer projection/reverse resolution, then repair #21's
   BC recipes and extractors using the same contracts. Clear-cut erroneous code
   corrections can be delivered earlier with focused regression tests.
5. Seed additional/corrected field mappings and scoped answer rows for BC, then
   MM/FL/CLL and shared values; integrate genetics and existing therapy catalogs.
   Missing vocabulary is reported, never replaced with invented Athena rows.
6. Run scoped dry-run reconciliation and rollout checks. Reconcile pending edits,
   legacy strings and facts produced under corrected mappings separately. Preserve
   source data, user decisions and historical event dates; record mapping revision
   provenance and make retries idempotent and resumable.

The seed loader must be idempotent by semantic natural keys, preserve reviewer
decisions, refuse invalid approved targets and emit counts for proposed, mapped,
unmapped, ambiguous, retired and missing-vocabulary rows. Deployment must not
depend on access to staging: ship reviewed manifest data and resolve against the
destination's loaded vocabulary. An export/import must re-resolve vocabulary/code,
not copy local IDs or reviewer user IDs between environments.

Tests must cover model constraints and permissions; no-equivalent choices;
alias collisions; non-standard/deprecated/missing targets; many answers sharing
one standard concept; context ambiguity; save/projection/refresh/FHIR roundtrips;
multi-value facts; explicit clears; pending-edit preservation; non-destructive
mapping revisions; migration/transfer idempotency; and bounded query counts.

Close #26 only when every inventoried field and value has a reviewed disposition,
every approved mapping is used by the intended runtime path, retained local values
round-trip, and therapy integration reuses the reference tables. This does not
require pretending that every value has a standard equivalent.

Close #21 only when every row of its field matrix is supported or explicitly
resolved as a documented alias/linked/computed representation, its BC regression
tests pass, and scoped reconciliation verifies the repaired extraction. Creating
this plan and child issues does not itself close either implementation issue.

## 9. Implementation issues

The following implementation issues have been created. Existing work to
coordinate includes #1076 (disease mapping contracts), #1069 (aliases), #1071
(computed fields), #1072 (therapy episodes), #1078 (labs), #692 (suggestion audit),
#461/#623 (vocabulary provenance/distribution), #540/#668/#348 (genetics),
#525 (pre-existing conditions), and #1221 (sample-cohort staging).

| Issue | Work | Dependencies |
|---|---|---|
| [#1223](https://github.com/healthkey-ai/promop/issues/1223) | Field mappings: reconcile every field and enum value against staging Athena (#26, #21) | None |
| [#1224](https://github.com/healthkey-ai/promop/issues/1224) | Field mapper: add stable scoped choices and reviewed value-to-concept mappings | #1223 |
| [#1225](https://github.com/healthkey-ai/promop/issues/1225) | Field mapper: curate answer concepts in the existing choice editor and APIs | #1224 |
| [#1226](https://github.com/healthkey-ai/promop/issues/1226) | Project and read back approved field-value concepts through the existing backend save flow | #1224 |
| [#1227](https://github.com/healthkey-ai/promop/issues/1227) | Repair breast cancer staging, pathology and test metadata mappings and extraction (#21) | #1223, #1226 |
| [#1228](https://github.com/healthkey-ai/promop/issues/1228) | Seed missing MM, FL, CLL and shared field questions and all enumerated answer mappings | #1223, #1224, #1226 |
| [#1229](https://github.com/healthkey-ai/promop/issues/1229) | Map genetic catalog values to Athena concepts with gene and variant context | #1223, #1224, #1226 |
| [#1230](https://github.com/healthkey-ai/promop/issues/1230) | Expose therapy catalog mapping coverage and resolve gaps using existing regimen/component/class tables | #1223, #1224 |
| [#1231](https://github.com/healthkey-ai/promop/issues/1231) | Transfer, reconcile and verify field and answer mappings without losing historical or unmapped values | #1224, #1225, #1226, #1227, #1228, #1229, #1230 |

The #21 implementation is tracked by #1227 and depends on the shared inventory
and answer-resolution work. #1231 supplies the final reconciliation and rollout
checks for both parent issues.
