# PRomop Genomics: Implementation Plan

The implemented baseline includes [#1249](https://github.com/healthkey-ai/promop/pull/1249), [#1259](https://github.com/healthkey-ai/promop/pull/1259), [schema audit #1263](https://github.com/healthkey-ai/promop/pull/1263), [PALB2 naming #1281](https://github.com/healthkey-ai/promop/pull/1281), and [project compatibility #1294](https://github.com/healthkey-ai/promop/pull/1294). Updated 2026-09-14 with catalog/write validation consistency and Render staging verification scope. The [implemented architecture](genomics_architecture.md) describes runtime behavior; this plan records delivery status, remaining requirements and acceptance criteria. The supplied Word documents are retained as requirements through these two canonical documents.

## Release 1.3 requirements

The 1.3 scope is manual genomic finding entry, editing, state readback and source provenance. Reports/specimens/scope (#1243/#1244), incremental imports (#1245), expanded result mappings (#1229), and new clinical derivation rules (#1246) are deferred until after 1.3. Existing unvalidated derivations remain inactive. The schema backfill in #1237 is conditional; current active Render audits do not indicate a need for it.

| Required gate | Issue | Current evidence and remaining work |
| --- | --- | --- |
| TP53 consumer compatibility and cache reconciliation | [#1315](https://github.com/healthkey-ai/promop/issues/1315) | EXACT is the only downstream consumer in scope. [EXACT #483](https://github.com/healthkey-ai/exact/pull/483) merged after all backend CI groups passed; it preserves explicit true/null through adaptation, normalization and eligibility matching. PRomop's scoped reconciliation command and tests are implemented in the release-gate branch; staging preview/apply receipts and compatible production rollout remain. |
| Production upgrade and writer prerequisites | [#1316](https://github.com/healthkey-ai/promop/issues/1316), [#1286](https://github.com/healthkey-ai/promop/issues/1286) | Render staging passes the combined release preflight. Production has 53 pending migrations, including genomics fields and mapping provenance. The chain applied successfully to an isolated copy of production schema and selected reference metadata, with no clinical rows copied. Production also lacks required EHR actor concept 32817, confirmed by a read-only vocabulary query. Load it from the approved Athena release through the vocabulary maintenance process; do not fabricate a replacement. Production deployment and its final combined audit remain; a rehearsal is not a live upgrade. |
| Final candidate smoke verification | [#1317](https://github.com/healthkey-ai/promop/issues/1317) | The authenticated smoke runner passes locally, including interrupted-write cleanup. Run against a dedicated synthetic record on Render staging after the final candidate is deployed to both web and worker; retain SHA, CRUD/state/provenance/readback results and artifact accounting. |

These three gates are required before declaring genomics ready for 1.3. A code merge alone does not complete an operational gate. Verify EXACT's deployed consumer revision includes its fix before enabling the new aggregate there. Cloud Run verification is outside this release work.

## Delivery status

| Work | Issue | Current state |
| --- | --- | --- |
| Owned NOTE references and bounded projection reads | [#1236](https://github.com/healthkey-ai/promop/issues/1236) | Closed; merged in #1249 |
| Shared text-column schema reconciliation | [#1237](https://github.com/healthkey-ai/promop/issues/1237) | Open; read-only audit merged in #1263; both active Render databases have narrow columns and zero overflow; no current width remediation indicated |
| Writer mapping/concept prerequisite audit | [#1300](https://github.com/healthkey-ai/promop/issues/1300) | Implemented; Render staging passed the combined check; production prerequisites remain in #1286 |
| Catalog/write validation consistency | [#1310](https://github.com/healthkey-ai/promop/issues/1310) | Implemented; catalog flags share the writer’s parent storage checks; invalid mappings remain visible without edit controls |
| Complete component/curation registry | [#1238](https://github.com/healthkey-ai/promop/issues/1238) | Closed; merged in #1249 |
| Portable variant-name recipe and conclusive domain audit | [#1239](https://github.com/healthkey-ai/promop/issues/1239) | Closed; merged in #1249; installed-vocabulary audits remain an operational requirement |
| Effective finding state and shared dialog | [#1240](https://github.com/healthkey-ai/promop/issues/1240) | Core implementation merged in #1249; true/unknown TP53 correction merged in #1297; clinical applicability, negative/combined aggregation and consumer review remain |
| Assertion provenance and versioned derivation boundary | [#1241](https://github.com/healthkey-ai/promop/issues/1241) | Closed; merged in #1249; clinical derivation remains disabled |
| One cytogenetic editor and original legacy history | [#1242](https://github.com/healthkey-ai/promop/issues/1242) | Closed; merged in #1259 |
| BIDMC test/specimen/scope/volume agreement | [#1243](https://github.com/healthkey-ai/promop/issues/1243) | Open; source examples and decisions required |
| Shared tests, specimens and bounded sequencing projections | [#1244](https://github.com/healthkey-ai/promop/issues/1244) | Open; implementation depends on #1243 |
| Idempotent source import and extraction provenance | [#1245](https://github.com/healthkey-ai/promop/issues/1245) | Open; identity and report-version contract depends on #1243; live imports remain gated |
| Reviewed PALB2 naming and compatibility | [#1275](https://github.com/healthkey-ai/promop/issues/1275) | Implemented: catalog v2, forward projection/curation migration and source-preserving aliases |
| Reviewed clinical catalog and derivation decisions | [#1246](https://github.com/healthkey-ai/promop/issues/1246) | Open; PALB2 naming approved and implemented in #1275; other expert/source decisions remain |

Keep Measurement finding parents, linked CDM components, approved mappings, the 42 marker projections and the frozen source catalog. Test/specimen support will extend existing CDM tables. Do not introduce G-CDM tables, replay retired branch instructions, or change source examples without reviewed decisions. PALB2 naming now has an explicit reviewed decision; its implementation preserves the historical source spelling. Storage approval does not confer clinical validation.

## Next implementation and operational work

### 1. Reconcile deployed shared text columns (#1237)

The owned-NOTE writer and batched reader are complete. Migration `0222_genomics_text_values` was edited into a no-op; a recorded migration name cannot establish which historical operations ran. There is still no forward backfill or narrowing migration.

Implemented now: `python manage.py audit_genomics_schema --environment <deployment> --check` reports the actual Measurement/Observation `value_as_string` types, character limits, complete row/overflow counts, maximum lengths and the recorded migration timestamp. It uses one database-enforced read-only, repeatable-read transaction with a configurable statement timeout. JSON contains metadata and aggregate counts, not patient identifiers or source text. Missing/unsupported columns fail `--check`; query failures or row-security restrictions produce no partial success report. A passing width check does not verify NOTE ownership or historical migration operations. See the [architecture's operator procedure](genomics_architecture.md#long-text-and-schema).

Read-only Render audits on 2026-09-14 verified that the two active deployment databases, `promop-dev` (Render staging) and `promop-db` (Render production), have `varchar(60)` Measurement/Observation columns and zero oversized values. [Retained audit evidence](https://github.com/healthkey-ai/promop/issues/1237#issuecomment-5660440818) includes an additional legacy deployment that the owner subsequently confirmed unused; its web service has been removed and it is excluded from active readiness work.

No narrowing/backfill is indicated for the active Render databases. These audits do not certify historical migration operations or NOTE ownership. Use Render staging for verification; Cloud Run access is outside the current genomics delivery gates.

Remaining work:

1. Repeat the inventory and audits when deployments or schemas change. Active Render services are `promop` / `promop-worker` on `main` and `promop-staging` / `promop-staging-worker` on `dev`. Review recorded migration history alongside actual widths and overflow counts.
2. If a formerly widened deployment is found, identify every consumer of affected genomic and non-genomic text before choosing an owned-NOTE encoding. The shared columns contain more than genomics; rewriting them to references before their readers support those references would lose usable source text.
3. Implement and test a forward, resumable backfill only for the evidenced schema/data paths. Preserve original values and dates, patient/fact/context ownership, ambiguous or missing legacy references, and historical notes. Do not manufacture clinical assertions to hold overflow.
4. Narrow only after lossless readback, reader compatibility and a final zero-overflow check are demonstrated under a strategy that handles concurrent writes. Apply deployment remediation as an explicit operational step; do not edit deployed migration history again.

Acceptance remaining: fresh and formerly widened paths converge without losing 60/61/10,000-character or non-genomics text; maximum NOTE IDs, missing/ambiguous references, retries after partial backfill and concurrent writes are handled. Current audit tests demonstrate detection and nonmutation of widened schemas; they do not claim a backfill exists.

### 2. Verify installed recipes and vocabulary

The effective registry exposes 31 components; all 73 parent/component recipes seed idempotently. Recipe v3 promotes only exact untouched variant-name seeds to LOINC 81253-7 while preserving curator decisions and local source identity. Domain resolution and audit tooling are implemented.

The 2026-09-14 09:36 UTC read-only Render staging audit passed both component domains and the new writer-prerequisite checks. Both staging web and worker configurations were verified against the audited database. See the [retained audit evidence](https://github.com/healthkey-ai/promop/issues/1286#issuecomment-5661997210). Installed metadata reports LOINC 2.80, vocabulary release `v5.0 29-AUG-26`, and UCUM 1.8.2. SNOMED identifies itself as a synthetic benchmark seed; the component audit is not broad clinical vocabulary certification.

Render production `promop-db` passed the width audit but could not run the component audit because it lacks `field_concept_mapping.provenance`. [#1286](https://github.com/healthkey-ai/promop/issues/1286) tracks that migration prerequisite and verification before enabling the current genomics writer there. Its metadata reports LOINC 2.82 and `v5.0 29-AUG-26`. The unused legacy Render deployment is outside this work. No live database, mapping or vocabulary changes were made during these audits.

For each deployment, run `audit_genomics_domains --include-writer-prerequisites` after vocabulary loading and retain its output with the vocabulary version. The new option also checks all 42 effective priority parent recipes, active concept 0, the default clinical/service (32817) and patient/representative (32865) actor concepts, and the same CDM event identity used by the writer. Missing/rejected/incomplete parent recipes, non-Measurement parents and missing/retired required concepts fail the combined audit. Without the option, the command retains its component-only scope. The latest Render staging receipt includes this option; older component-only receipts and the production width audit do not establish writer prerequisites. Resolve incomplete/ambiguous codes and table/domain mismatches through field-mapping curation, retaining source/value/unit settings. Rerun until verified. Curation changes future writes; moving existing clinical facts between tables requires a separately reviewed repair.

The prerequisite check performs metadata reads only and does not certify database permissions, sequences, schema completeness, custom service type IDs, every possible write payload, clinical semantics or a deployment. Parent concept 0 remains supported when the installed standard concept is in Observation or no usable Measurement concept exists; source identity is retained. Complete the width audit and deployment migration checks separately. #1286 stays open until the required deployment evidence exists.

Retain independent transcript DNA change (48004-6), genomic DNA change (81290-9), protein change (48005-3), protein change type (48006-1), and raw variant text. Coverage depth (82121-5) currently belongs to each finding; source granularity remains to be agreed. Clone fraction has a local numeric recipe, independent of VAF. Obtain the source's actual status answer set for 69548-6. Retain chromosome 48000-4 rather than substituting the supplied 73822-9 identifier.

### 3. Finish clinical state and consumer semantics (#1240, #1246)

Effective state, contradiction rejection, inherited-component clearing, unknown placeholders, all shared dialog controls, and stale detected-summary clearing are implemented. Legacy no-call/not-tested/unrecognized assessments remain source-preserving indeterminate results. Interactive omitted units still default to percent; future imports must provide explicit units.

Remaining decisions and implementation:

- Agree marker-specific absence and no-call/not-tested/below-threshold semantics, including clone-fraction applicability. Review the complete absent-component matrix rather than assuming one gene-negative result establishes structural absence.
- Define evidence-aware TP53/del(17p) aggregation. [PR #1297](https://github.com/healthkey-ai/promop/pull/1297) merged the release-owner-approved true/unknown correction: version 8 preserves the existing positive predicate and emits null otherwise. It does not establish negative-result criteria, add del(17p), or resolve contradictory sources. Follow its [rollout and consumer contract](tp53_aggregate_plan.md) before rederiving deployed caches; the read-only cache estimate is not a completed backfill.
- Inventory and update downstream trial/eligibility consumers against the agreed contract. Validate absent, indeterminate, no-evidence and contradictory-source cases before closing #1240.

Do not add clinical thresholds or activate complex-karyotype derivation before review. Retain existing assertions, source wording and superseded history through any approved transition.

### 4. Agree source requirements before Phase 2 (#1243)

Obtain concrete deidentified examples and decisions for:

- Discrete calls versus raw sequence files; calls per report and reports per patient; benign/reference calls; one-way ingestion versus readback.
- Explicit negatives and assay scope: gene lists, versioned panels, region/BED references or FISH probes; callable-result evidence and reporting/filtering limitations.
- Report/accession namespaces, stable identities, versions, amendments/retractions; specimen and matched-normal identifiers; mandatory clinical dates.
- Coverage granularity, interpretation versus clinical significance, origin versus genomic source class, and actual status answer sets.

Record the decisions on #1243 and link the receiving contract. Do not scaffold guessed test models or fabricate context while these requirements remain unresolved.

#### Proposed source contract for review

The following is a decision template for #1243, not an agreed BIDMC interface or an implemented receiving API. Store deidentified examples in an approved location and link them from the issue; do not put patient data in GitHub. For each row, record the source's actual field/path and example, the chosen rule, the decision owner and date, and the receiving acceptance test. Unresolved rows remain explicit blockers for the capabilities they affect.

| Contract area | Proposed receiving behavior | Source decision/evidence needed |
| --- | --- | --- |
| Patient and source identity | Scope report identity by organization, source system and patient; reject cross-patient reassignment. | Authoritative patient matching, accession namespace, stable report ID and collision examples. |
| Report versions and finding identity | Preserve prior versions; use stable source finding IDs within the agreed report identity. Replaying the same version should not duplicate facts; changed content under the same identity should require reconciliation. | Full replacement versus incremental amendments, version ordering, retractions, stable finding IDs and behavior when IDs are absent. |
| Dates and specimen links | Keep collection, assay and report dates distinct; preserve available specimen and matched-normal identifiers. Hold findings without the clinical date required by the receiving contract. | Required dates, precision/timezone, specimen identity/type/site, pairing, and examples with missing or partial context. |
| Finding state and assay scope | Preserve explicit source calls, including no-call and indeterminate wording. Retain zero-finding reports and scope separately; unreported scope members stay unknown unless a reviewed policy supports inference. | Actual status answer sets, explicit negative examples, versioned panel/probe/region definitions, callable evidence and reporting limitations. |
| Numeric and interpretation context | Require numeric units; keep VAF and clone fraction separate, and preserve source interpretation without adding clinical thresholds. | Fraction versus percent conventions, coverage granularity, origin/source class and significance/tier/actionability fields. |
| Payload and volume | Receive discrete report/finding content with bounded patient projections and access to findings outside those projections. | Ingest format, typical and largest report/patient counts, benign/reference-call inclusion, raw-file retention and readback requirements; agree numeric limits before implementation. |
| Extraction and reconciliation | Retain source document/version and extraction/reviewer references; apply a whole finding atomically. Retry after a committed write should resolve to the same result. | Authoritative document identity, extractor rerun behavior, review/hold decisions, report-level transaction boundary and partial-failure response. |

The minimum source example set should cover an ordinary positive report, an explicit negative report, a no-call or indeterminate result, a zero-finding report with scope, a multi-specimen or matched-normal report, an amendment and retraction, and a realistic high-volume report. Include missing-date/unit/identity cases and repeated deliveries to define rejection, hold and retry behavior. If a case is unsupported by the source, record that explicitly instead of inventing an example or a clinical rule.

Convert the accepted examples into fixtures and contract tests under #1244/#1245. Until acceptance is recorded, existing interactive list replacement remains the only implemented list-write behavior; it must not be used as an incremental source import.

### 5. Implement tests, specimens and bounded projections (#1244)

After source agreement, use SPECIMEN for available specimen identity/type, collection and site. Define homes for purity and tumor/normal pairing. Represent the assay as a Measurement parent with report/accession, panel/version, method, reference build, coverage summary, issue date, status, supersession, laboratory and limitations as components. Specify the test/specimen relationship and link findings through the existing event mechanism; preserve both literal CDM codes and Athena-name identity resolution.

Use owned NOTE text for narrative reports. Preserve tests with zero findings and store scope as a definition/reference. Migrate duplicated per-finding context with source-preserving compatibility. Do not fabricate a procedure, specimen or clinical date.

Explicit findings take precedence over scope inference. Scope membership alone cannot turn unreported/filtered variants into negatives. Only a reviewed assay policy with scope, limitations and callable evidence may support derived absence; keep it projection-only with version/time/test provenance, never as an observed fact.

Define which findings enter PatientRecord, a cap and overflow behavior for `genetic_mutations`, and pagination/query access to remaining OMOP findings. Whole-test imports must suppress per-finding refresh and rebuild once. Validate realistic source volumes. Raw FASTQ/BAM/CRAM stay outside OMOP; use pointers only if retention is required. VCF may be an ingest format.

Acceptance: report versions, specimen links, zero-finding reports and scope round-trip without invented context; explicit evidence wins; projection size and refresh cost remain bounded.

### 6. Implement receiving capabilities and reconciliation (#1245)

Publish a versioned finding/test action schema and capability contract covering fields, states, provenance, authorization, atomic whole findings, and partial-failure behavior. Align source identity with #1243/#1244: organization, patient, source system, report ID/version and stable finding identity.

Implement concurrent retry, timeout-after-commit and extractor-rerun reconciliation; amendments and retractions; persistent DOCUMENT_EXTRACTION document/extraction/reviewer references. Rich source spans remain source-owned. Hold undated findings until a reviewed undated representation exists; upload/extraction time must not become a clinical test date. Require numeric units.

A specialized whole-finding review/hold adapter must gate live imports until receiving capabilities are deployed. General/named-list replacement is not incremental import. Generic FHIR observation sync is not a validated finding/component/report adapter; any FHIR Genomics adapter needs its own contract and round-trip tests.

## Field/value mapping coordination

[ADR 0001](adr/0001-vocabulary-source-of-truth.md) defines vocabulary authority
and the separation of vocabulary release, reviewed mapping revision and source
catalog/recipe version. The
[field/value plan](field_concept_mapping_plan.md#vocabulary-distribution-and-mapping-provenance)
owns shared distribution and answer-mapping delivery. This section owns the
Genomics-specific acceptance; ADR 0002's therapy-class overlap rules do not
establish genomic eligibility semantics.

Coordinate **#1229 after #1223/#1224/#1226** with the existing recipe audit and
clinical/source work in #1240/#1243/#1245/#1246:

1. Inventory stable marker/component identities and scoped status, origin,
   interpretation and variant values without changing the frozen v1 catalog.
   Keep field questions, coded answers and whole findings distinct. Preserve
   gene, transcript/build, method, source text and unknown/no-equivalent cases.
2. Record installed vocabulary release evidence separately from each reviewed
   parent/component/answer recipe revision and source catalog version. Recipe v3
   is not a new catalog version or an Athena release. Resolve portable codes and
   domains on the destination, rerun `audit_genomics_domains`, and retain curator
   approvals/rejections and independent source/value/unit settings.
3. Extend the common mapping interface/resolver through the existing structured
   writer. Retain Measurement finding parents and linked Measurement/Observation
   components; never substitute a flat answer list or G-CDM storage. A new answer
   mapping must not change source assertion, effective finding state or clinical
   interpretation without the corresponding reviewed contract.
4. Under #1245, make import/reconciliation evidence identify source report/finding
   version plus the mapping and vocabulary evidence used. Verify round trips,
   repeat imports and amendments without changing historical source text/dates.
   Correcting a recipe affects future writes; repairing existing facts is scoped,
   separately reviewed work, not an automatic consequence of vocabulary sync.

Do not close #1229 merely because recipes seed or a vocabulary loads. Require
reviewed value dispositions, structured write/readback coverage and the agreed
clinical state/consumer tests. Test/specimen identity, derivation activation and
source receiving capabilities retain their existing gates above.

## Clinical and external coordination

Track on #1246: definitions of both complex-karyotype markers and counting/grouping by test/date; marker-specific absence; source example validity; disease subsets; TP53/del(17p) evidence aggregation; dated interpretation history; FISH/karyotype/clone-fraction validation. Distinguish pathogenicity, somatic evidence tier and therapeutic actionability if the source requires them.

Coordinates/alleles/external identifiers, copy number, fusion breakpoints and read counts depend on source requirements. TMB/MSI/HRD should be assay results when required. A read-only G-CDM projection remains deferred and does not replace canonical storage.

- CDEW [design PR #101](https://github.com/healthkey-ai/cdew/pull/101): coordinate the receiving contract with `vtrv101` (Vlad). Its merge/deployment is separate from PRomop delivery.
- CancerBot [PALB naming #4812](https://github.com/cancerbot-org/cancerbot/issues/4812): Samar [approved Option A on 2026-09-11](https://github.com/cancerbot-org/cancerbot/issues/4812#issuecomment-5633297037). PRomop #1275 implements the canonical PALB2 field/code, legacy aliases and migration of patient projections/curation identities while preserving original clinical source rows. CancerBot's own patient/trial migration is separate and remains open.
- CancerBot [example validation #5297](https://github.com/cancerbot-org/cancerbot/issues/5297): Samar has begun review but has not posted findings. [Disease subsets #5298](https://github.com/cancerbot-org/cancerbot/issues/5298): she confirmed disease-specific lists and will provide explicit subsets. Neither reply supplies the outstanding example corrections or subset membership; retain current values.
- PRomop [#1229](https://github.com/healthkey-ai/promop/issues/1229): coordinate reviewed result concepts and its #1223/#1224/#1226 dependencies. Component-question curation must not flatten structured findings into answer labels.

## Verification and delivery

Use explicitly configured isolated local PostgreSQL databases for tests. Use Render staging for operational verification and confirm its configured database. Production promotion remains a separate release step. Never inherit an unspecified remote `.env` database URL in tests.

Retain meaningful regressions for event/patient isolation, owned NOTE history, state transitions, independent components, source-preserving legacy reads, assertion/derivation separation and query budgets. For migration/backfill changes, exercise fresh and affected prior schema/data plus interrupted retries. Complete required GitHub CI before merge and update these two documents as capabilities land.

Merged verification is attached to [#1249](https://github.com/healthkey-ai/promop/pull/1249) and [#1259](https://github.com/healthkey-ai/promop/pull/1259), including full required CI. The new schema audit is covered by [PostgreSQL schema-variant tests](../tests/test_genomics_schema_audit.py): actual narrow/widened/missing/unsupported columns, character counts, read-only enforcement, rejection of row-filtered counts, repeatable-read isolation, safe retries, timeout configuration and sanitized failures. These tests do not certify live vocabulary or uninspected deployments.

PALB2 verification covers legacy and canonical field/marker inputs, conflicting lists, original-gene metadata, unchanged echoes, repeated finding IDs, rejected/curated mappings and idempotent seeding. The naming correction does not validate source examples, backfill CancerBot trial data or activate clinical derivations.
