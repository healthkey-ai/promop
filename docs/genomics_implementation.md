# PRomop Genomics: Remaining Implementation Plan

See [the implemented architecture](genomics_architecture.md) for current behavior and source links. Updated from `dev` at `07a1cc4` with the #1242 ownership implementation on 2026-09-13; the supplied Word documents are source material, not a claim that every proposed feature exists. This is a work plan for remaining requirements.

## Issue tracking

| Work | Issue | Status |
| --- | --- | --- |
| secure owned NOTE overflow references and bound projection reads | [#1236](https://github.com/healthkey-ai/promop/issues/1236) | Merged in [#1249](https://github.com/healthkey-ai/promop/pull/1249) |
| reconcile previously widened shared text columns without data loss | [#1237](https://github.com/healthkey-ai/promop/issues/1237) | Open |
| expose all implemented components through one curation registry | [#1238](https://github.com/healthkey-ai/promop/issues/1238) | Merged in [#1249](https://github.com/healthkey-ai/promop/pull/1249) |
| promote variant-name LOINC recipe and make domain audits conclusive | [#1239](https://github.com/healthkey-ai/promop/issues/1239) | Merged in [#1249](https://github.com/healthkey-ai/promop/pull/1249); deployment audit pending |
| unify finding state, complete dialog fields and preserve unknown evidence | [#1240](https://github.com/healthkey-ai/promop/issues/1240) | Core state/UI implemented; clinical aggregate and consumer review remain |
| make asserted projection provenance editable and derivation metadata consistent | [#1241](https://github.com/healthkey-ai/promop/issues/1241) | Merged in [#1249](https://github.com/healthkey-ai/promop/pull/1249) |
| reconcile cytogenetic summaries and centralize overlapping finding editing | [#1242](https://github.com/healthkey-ai/promop/issues/1242) | Implemented: Genomics owns entry; original legacy history remains readable |
| agree BIDMC test, specimen, scope and volume requirements | [#1243](https://github.com/healthkey-ai/promop/issues/1243) | Source agreement required |
| implement shared tests, specimens, scope and bounded sequencing projections | [#1244](https://github.com/healthkey-ai/promop/issues/1244) | Gated on source agreement |
| publish and implement idempotent source import and extraction provenance | [#1245](https://github.com/healthkey-ai/promop/issues/1245) | Open |
| track reviewed clinical catalog, state and derivation decisions | [#1246](https://github.com/healthkey-ai/promop/issues/1246) | Expert/source decisions required |

## Direction and scope

Keep the finding Measurement plus linked CDM components, approved mapping model, 42 marker projections and frozen catalog. Do not introduce G-CDM tables or a VARIANT_OCCURRENCE-shaped replacement. The target design rejects that replacement because it does not naturally cover the catalog's structural/genome-wide markers and assumes procedure/specimen context that some incoming reports lack. There is no active G-CDM model to revert; do not replay historical stash, branch or PR instructions.

Status, clone fraction, transcript DNA change, per-finding coverage depth, amino-acid change type, NOTE overflow and asserted complex-karyotype projection stubs already exist. Finish and verify them rather than implementing them again. Do not invent complex-karyotype thresholds or change PALB1/source examples without clinical review. Storage mapping approval does not confer clinical validation.

Test/specimen/scope support is a planned second phase gated on source requirements. It is outside the immediate finding-contract work and remains on the product roadmap.

## Phase 1: complete the finding contract

### 1. Secure NOTE references and reconcile schema history

Runtime ownership and batched reads are implemented in this change (#1236). Deployment schema and ambiguous legacy-data reconciliation remain #1237. The gap analysis below records the motivation and full acceptance criteria; see the architecture for the resulting reference contract.

Observed gap. The writer already stores overflow in NOTE, and models now limit strings to 60 characters. Migration 0222 was edited into a no-op. This does not narrow databases that previously applied its widening operations, and the repository alone cannot establish whether those databases contain overflow. Before #1236, the NOTE reader fetched trailing NOTE IDs without ownership checks and interpreted literal reference-shaped text. Owned references and conservative legacy reads now address the runtime gap; deployment schema and legacy ambiguity still require reconciliation.

Work. Make reads require the owning patient and finding/component identity. Define unambiguous reference handling, including literal reference-shaped source text, missing notes, note supersession and retained history. Reuse the ownership principles already implemented in services/cytogenetics.py; provide a common CDM-table mechanism suitable for future test reports without losing legacy text. Batch or cache note reads during projection. Keep the 10,000-character input limit and avoid creating clinical assertions merely to hold overflow.

Inspect migration history and actual column types in each affected deployment before choosing remediation. If any database applied the old widening, use a forward, resumable migration/backfill that copies overflow losslessly into owned notes before narrowing. Account for non-genomics values because the widened columns were shared. Do not edit deployed history again or narrow away data.

Acceptance. Fresh and previously widened schema paths converge; 60/61/10,000 character inputs, large NOTE IDs and retry after partial backfill preserve text. Cross-patient and wrong-fact references cannot resolve; edits/deletion preserve intended history; repeated refresh has bounded NOTE query cost. Describe remaining local schema/reference conventions accurately rather than claiming blanket CDM conformance.

### 2. Complete portable component recipes and curation

The 31-field effective registry and curation visibility are implemented (#1238). Recipe v3 and the conclusive audit implement #1239 without rewriting historical seed inputs. Exact untouched variant-name seeds gain LOINC 81253-7 metadata while retaining the local source key; curator and rejected mappings are preserved. Reads and edits recognize both local and portable component codes.

The shared vocabulary resolver requires an unambiguous active source and standard target, including Maps to. Absent vocabulary retains concept 0/raw source. The audit checks every component recipe and fails for missing approvals, unresolved/ambiguous codes and domain drift; it prints explicit repairs for the existing curation workflow. Tests cover loading vocabulary before and after promotion, legacy/coded reads, curator preservation and the four Observation-domain corrections.

Deployment action remains: run the audit against the installed vocabulary after loading it, review each proposed mapping correction in field-mapping curation, preserve source/value/unit settings, and rerun until verified. Retain the audit output and vocabulary version. No deployment vocabulary has been certified by fixture tests, and no existing clinical facts are rewritten by a recipe repair. Clinical/source review of answer sets and depth granularity remains open.

| Requirement from references | Current state / remaining action |
| --- | --- |
| Status, 69548-6 | Seeded; verify installed mapping and obtain BIDMC answer set |
| Clone fraction | Local numeric recipe exists; document vocabulary search and retain fallback if no suitable code |
| Variant name, 81253-7 | Version 3 promotion and compatible reads implemented; nonmatching old recipes require curation |
| Transcript DNA change, 48004-6 | Exists; keep distinct from genomic DNA change, 81290-9, and preserve raw variant |
| Coverage depth, 82121-5 | Per-finding number exists; verify domain and source granularity |
| Amino-acid change type, 48006-1 | Exists; keep distinct from protein change, 48005-3 |
| Chromosome, 48000-4 | Retain; do not replace with the BIDMC-list 73822-9 identifier |

Acceptance. All component mappings are discoverable and editable/rejectable through curation. Seed retries preserve curator decisions. Tests with vocabulary absent, loaded before seeding and loaded afterward cover domain resolution and legacy reads. Variant name round-trips through its coded recipe; paired DNA and protein fields remain independent.

### 3. Make status consistent across writes, presentation and consumers

Implemented on the finding-state branch for #1240: one effective state uses explicit status first, then source-preserving legacy assessment compatibility. No-call/not-tested and unrecognized nonempty source assessments project as indeterminate. Only findings with neither field retain default-present compatibility. New contradictions are rejected, legacy assessment-only edits continue working, and state-only transitions preserve superseded assessment history. There is no bulk backfill or invented negative assertion.

The absent guard now includes transcript DNA change, amino-acid change type and zygosity; inherited incompatible values are cleared while raw tested-scope text and coverage context remain. The shared dialog exposes the previously missing controls, separate VAF/clone-fraction units and original/transcript/genomic/protein text fields, one editable status, read-only source assessment and an effective-status list column. Empty priority rows display Unknown and create no facts. Interactive percent-unit defaults remain for compatibility; future import contracts must supply units explicitly.

Backend tests cover general/named/dedicated state transitions, legacy source preservation, contradiction rejection and stale detected-summary clearing. Shared frontend tests exercise independent controls, unknown/absent/indeterminate display and clearing disabled fields on absent edits.

Still open in #1240/#1246: clinical/source agreement on no-call/not-tested/below-threshold semantics, complete absent applicability (including clone fraction), and an evidence-aware combined TP53/del(17p) aggregate. The legacy aggregate still returns false without qualifying evidence; a gene-only negative must not establish structural absence. Review downstream trial-matching consumers with that agreed contract before closing this issue. No clinical thresholds or aggregate derivation have been added.

### 4. Finish provenance plumbing, leaving derivation disabled

Implemented for #1241: both general and named lists consistently identify assertions. Existing asserted projection echoes can be edited while client-authored derived metadata is rejected. A typed, versioned derivation result gains a timestamp at projection time; the two clinical stubs remain disabled. Derivation receives copies of canonical observed inputs and cannot feed previous derived lists back into its inputs.

The provenance registry and source lookup now describe and return actual finding Measurements and recognized linked components, including safe NOTE text, across the general and all named fields. The three interactive write paths select the same actor type, and parent/component edits retain the current writer's type consistently.

Acceptance retained for future changes: asserted echoes round-trip; derived writes create no facts; assertions bypass derivation; synthetic derived output has version/time and no stored parent ID; derivation has no feedback from previous projections. Clinical rules and extraction provenance remain separate work.

### 5. Close linkage and overlapping-editor inconsistencies

Completed linkage work: [PR #1233](https://github.com/healthkey-ai/promop/pull/1233) shares event-concept identity across reads, writes, supersession and deletion and parametrizes CRUD tests for literal codes and Athena names. Retain this regression coverage. Overlapping-editor ownership is now implemented in #1242: the Disease summary is read-only and Genomics owns new discrete entry.

Preserve the shared resolver and its patient/event-type isolation when extending report/test linkage.

Implemented reconciliation contract: retain the legacy summary as a compatibility read and show original, paginated individual/aggregate facts in Genomics for comparison before manual entry. Preserve source text and dates, expose historical clears/errors as history, and show cache-only summaries without a date. Reject changed legacy summary PATCHes while ignoring unchanged autosave echoes. Do not infer negative findings from deselection, synthesize dates, or automatically convert or merge historical prose. Gain and amplification remain distinct where the source distinguishes them; ambiguous wording stays ambiguous. Cytogenetic risk classification remains a separate clinical field. Automated source identity/deduplication belongs to #1245 after the source agreement; clinical interpretation remains #1246.

Acceptance. Both event-concept encodings support read/edit/delete round trips without duplicate active components. Existing individual and aggregate cytogenetic data remain readable; the UI has a documented owner for overlapping findings and cannot silently maintain contradictory copies.

## Phase 2: tests, specimens and scope in existing CDM tables

This phase follows completion of the immediate finding contract and agreement on source requirements. Do not add partial test-model scaffolding to Phase 1.

Resolve these BIDMC requirements before finalizing the contract:

- Whether full sequencing means discrete calls or raw sequence files; variants per report, reports per patient, and whether benign/reference calls are sent.

- Whether BIDMC needs query/readback as well as one-way ingestion.

- Whether scope is needed alongside explicit negatives, and its source form: gene list, versioned panel definition, region/BED reference or FISH probe set.

- Report/accession identifiers and namespaces, amendment/retraction behavior, specimen and matched-normal identifiers, and mandatory dates.

- Coverage-depth granularity; interpretation versus clinical significance; origin versus genomic source class; and the status code's actual answer set.

Build specimen → test → findings → components using existing CDM tables. Use SPECIMEN for available specimen identity/type, collection and site information; define appropriate component homes for purity and tumor/normal pairing. Represent the assay as a Measurement parent under an appropriate panel/report source code, with accession, panel/version, method, reference build, coverage summary, issued date, report status, supersession, laboratory and limitations as components. Link findings to their test through event linkage; specify the test/specimen relationship using the available CDM mechanism before implementation. Do not fabricate a procedure or specimen to satisfy an unavailable source attribute.

Use the Phase 1 owned NOTE mechanism for narrative reports. Preserve tests with no findings. Store scope as a definition or reference, rather than expanding every possible gene into a negative finding. Migrate per-finding specimen context with source-preserving compatibility once the shared context is available.

Explicit findings of any status take precedence over a scope inference. Only infer absence where an agreed assay/reporting policy justifies it, with source scope, limitations and callable-result evidence. Scope membership alone must not turn an unreported or filtered variant into a negative. Such results remain projection-only, marked derived with version/time and test provenance; never write them back as observed findings.

Before large imports, define which catalog/clinically significant findings enter PatientRecord, an explicit cap and overflow behavior for genetic_mutations, and a query/pagination path to remaining OMOP findings. Make whole-test import suppress per-finding refresh and rebuild once at completion. Test realistic source volumes. Raw FASTQ/BAM/CRAM belong outside OMOP; store a pointer only if retention is required. VCF may be an ingest format, not the canonical storage representation.

Acceptance. Reports, amendments, specimen links, negative/no-finding reports and scope definitions round-trip without fabricated context. Explicit evidence wins over inference; derived absence is never persisted as an observed fact. Projection size and refresh work stay bounded on large imports.

## Receiving gates for CDEW and other automated sources

The old handoff correctly identified these requirements; they remain separate from interactive CRUD and must be complete before enabling live genomic imports:

- Publish a versioned finding/test action schema and capability contract, including supported fields, states, provenance and partial-failure behavior.

- Implement source identity and reconciliation using organization, patient, source system, report identity/version and stable finding identity. Cover concurrent retries, timeout after commit, extraction reruns, amendments and retractions. Named-list replacement is not an incremental import adapter.

- Persist extraction provenance and document/review references through a DOCUMENT_EXTRACTION adapter; keep rich source spans with their source system.

- Define missing-test-date handling. Hold undated source findings or support an explicitly agreed undated representation; never use upload/extraction date as a fabricated clinical test date.

- Implement the specialized receiving adapter and whole-finding review/hold behavior. Generic FHIR observation sync is not a demonstrated replacement for preservation of the finding/component/report graph; a FHIR Genomics adapter needs its own contract and round-trip validation.

Phase 1 can finish independently. Phase 2 and the receiving gates should share source identity and report-version decisions when whole tests become the target.

## Clinical decisions and deferred work

Keep these visible without turning them into unreviewed implementation rules:

- Definitions and thresholds for both complex-karyotype markers, including which observed findings count and how tests/dates are grouped.

- Which markers admit a meaningful absent state, and whether below-reporting- threshold results need a distinct state or contextual qualification.

- PALB1 naming, example validity and the five disease priority lists. The frozen fixture preserves the source; changes require reviewed, source-aware migration.

- Required report/specimen identifiers and interpretation history: distinguish dated pathogenicity, somatic evidence tier and therapeutic actionability if the source requirements warrant them.

- External validation for FISH, karyotype and clone fraction; the supplied BIDMC discrete-element list primarily exercises sequencing components.

Additional coordinate/allele identifiers, copy number, fusion breakpoints and read counts depend on source requirements. TMB/MSI/HRD should be modeled as assay results when required, not invented genes. A read-only G-CDM projection for the representable subset remains deferred and does not change canonical storage.

## Delivery and verification

Prioritize NOTE ownership and schema reconciliation, then component/curation and status completion, provenance and linkage fixes. Resolve the overlapping-editor contract before removing compatibility controls. Keep Phase 2 and import gates separately reviewable. Update the architecture as each change lands.

Use explicitly configured isolated local PostgreSQL databases for implementation validation; do not inherit a potentially remote DATABASE_URL from .env. Historical handoff hostnames, local ports and prior test totals are not evidence about the current environment.

Run the relevant genomics, legacy round-trip, domain, curation, cytogenetic and shared frontend suites, plus the refresh query-budget regression. For migration work, test both a fresh database and the affected prior schema/data, followed by makemigrations --check --dry-run. Add meaningful coverage for the acceptance criteria above, including a real installed-vocabulary event/domain fixture and NOTE-heavy projections. Complete the repository's required CI before merging implementation changes; do not reuse test-pass claims from the retired handoff.

## External coordination retained from the retired documentation

- CDEW [design PR #101](https://github.com/healthkey-ai/cdew/pull/101) describes the specialized finding contract and receiving gates; coordinate with `vtrv101` (Vlad). Its merge or deployment is not part of a PRomop implementation merge.
- CancerBot [PALB naming #4812](https://github.com/cancerbot-org/cancerbot/issues/4812), [example validation #5297](https://github.com/cancerbot-org/cancerbot/issues/5297) and [disease subsets #5298](https://github.com/cancerbot-org/cancerbot/issues/5298) were assigned to `SamarElkassas`. Preserve source strings pending reviewed answers.
- PRomop [#1229](https://github.com/healthkey-ai/promop/issues/1229) already tracks genetic catalog **answer** concept mapping and depends on #1223/#1224/#1226. Component-question curation here complements that work; it must not flatten a structured finding into answer labels.
- Initial interactive CRUD and catalog delivery was tracked by #1179/#1180/#1181 and PR #1188; old handoff stop conditions and uncommitted-work instructions have expired.

## Verification for the first implementation

On isolated PostgreSQL (`localhost:5433`, database name `promop_genomics_integrity`), the genomics NOTE, CRUD, catalog, domain and legacy round-trip suites passed: **186 passed, 1 skipped**. The one existing domain test skips tolerated differences in the frozen v1 fixture; seed resolution follows the installed concept domain. This is not a certification of a deployed vocabulary. Coverage includes both literal CDM event codes and Athena CDM-name fixtures, 60/61/10,000-character values, maximum signed-bigint NOTE IDs, patient/fact/context isolation, literal and missing references, legacy conversion on edit, retained history and one NOTE query per refresh. All five later components pass discovery, curation and seed-retry checks.

Schema-history reconciliation, live-source enablement and clinical decisions remain tracked in their open issues. This change does not certify deployed column widths or run live migrations.

Additional local checks passed: **145** mapping descriptor, provenance, cytogenetic compatibility and sample-data tests; **3** refresh query-budget tests after a fresh full migration chain; and `makemigrations --check --dry-run` reported no changes. PostgreSQL test databases were isolated from application data.

Provenance continuation: **208** genomics/provenance/catalog/legacy tests and **15** shared Genomics UI tests passed. The two additional composite cases and all 12 existing provenance registry/service/API and refresh-budget tests also passed.

## Verification for cytogenetic ownership (#1242)

The genomics/cytogenetics, NOTE, catalog, state, provenance, recipe and domain regression suite passed **387 tests with one existing skip**. A final run covering the new history endpoint, shared descriptors, projection reconciliation and legacy mutation round trips passed **120 tests**. These runs overlap and should not be added together. Coverage retains both literal-code and Athena-name event fixtures, original aggregate/individual text, gain/amplification ambiguity, historical clears/errors, patient/fact NOTE ownership, bounded pagination, cache-only undated summaries, and unchanged autosave echoes without fact writes.

All **67** Disease/Genomics/history UI tests passed, including retry, pagination and patient-switch isolation. TypeScript and targeted ESLint passed. A fresh full migration chain followed by the **12** Django provenance/API/refresh-budget tests passed on a separate isolated local database. The NOTE-heavy history page uses five queries regardless of the number of rows within a page. No schema migration or live-data conversion is introduced.
