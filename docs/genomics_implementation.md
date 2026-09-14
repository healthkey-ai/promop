# PRomop Genomics: Implementation Plan

Current baseline: `dev` at `25133b7`, including merged [#1249](https://github.com/healthkey-ai/promop/pull/1249) and [#1259](https://github.com/healthkey-ai/promop/pull/1259). Updated 2026-09-14 with the schema audit implementation below. The [implemented architecture](genomics_architecture.md) describes runtime behavior; this plan records delivery status, remaining requirements and acceptance criteria. The supplied Word documents are retained as requirements through these two canonical documents.

## Delivery status

| Work | Issue | Current state |
| --- | --- | --- |
| Owned NOTE references and bounded projection reads | [#1236](https://github.com/healthkey-ai/promop/issues/1236) | Closed; merged in #1249 |
| Shared text-column schema reconciliation | [#1237](https://github.com/healthkey-ai/promop/issues/1237) | Open; read-only audit implemented; staging evidence recorded; deployment inventory and any required remediation remain |
| Complete component/curation registry | [#1238](https://github.com/healthkey-ai/promop/issues/1238) | Closed; merged in #1249 |
| Portable variant-name recipe and conclusive domain audit | [#1239](https://github.com/healthkey-ai/promop/issues/1239) | Closed; merged in #1249; installed-vocabulary audits remain an operational requirement |
| Effective finding state and shared dialog | [#1240](https://github.com/healthkey-ai/promop/issues/1240) | Core implementation merged in #1249; clinical applicability, TP53/del(17p) aggregation and consumer review remain |
| Assertion provenance and versioned derivation boundary | [#1241](https://github.com/healthkey-ai/promop/issues/1241) | Closed; merged in #1249; clinical derivation remains disabled |
| One cytogenetic editor and original legacy history | [#1242](https://github.com/healthkey-ai/promop/issues/1242) | Closed; merged in #1259 |
| BIDMC test/specimen/scope/volume agreement | [#1243](https://github.com/healthkey-ai/promop/issues/1243) | Open; source examples and decisions required |
| Shared tests, specimens and bounded sequencing projections | [#1244](https://github.com/healthkey-ai/promop/issues/1244) | Open; implementation depends on #1243 |
| Idempotent source import and extraction provenance | [#1245](https://github.com/healthkey-ai/promop/issues/1245) | Open; identity and report-version contract depends on #1243; live imports remain gated |
| Reviewed clinical catalog and derivation decisions | [#1246](https://github.com/healthkey-ai/promop/issues/1246) | Open; expert/source decisions required |

Keep Measurement finding parents, linked CDM components, approved mappings, the 42 marker projections and the frozen source catalog. Test/specimen support will extend existing CDM tables. Do not introduce G-CDM tables, replay retired branch instructions, or change PALB1/source examples without reviewed decisions. Storage approval does not confer clinical validation.

## Next implementation and operational work

### 1. Reconcile deployed shared text columns (#1237)

The owned-NOTE writer and batched reader are complete. Migration `0222_genomics_text_values` was edited into a no-op; a recorded migration name cannot establish which historical operations ran. There is still no forward backfill or narrowing migration.

Implemented now: `python manage.py audit_genomics_schema --environment <deployment> --check` reports the actual Measurement/Observation `value_as_string` types, character limits, complete row/overflow counts, maximum lengths and the recorded migration timestamp. It uses one database-enforced read-only, repeatable-read transaction with a configurable statement timeout. JSON contains metadata and aggregate counts, not patient identifiers or source text. Missing/unsupported columns fail `--check`; query failures or row-security restrictions produce no partial success report. A passing width check does not verify NOTE ownership or historical migration operations. See the [architecture's operator procedure](genomics_architecture.md#long-text-and-schema).

[Render staging evidence](https://github.com/healthkey-ai/promop/issues/1237#issuecomment-5658503062), captured 2026-09-14 03:17 UTC, found both columns already `varchar(60)` and zero oversized values across 320,262 Measurements and 314,262 Observations. No narrowing/backfill is indicated for those observed columns. This result does not establish another deployment's state.

Remaining work:

1. Complete the inventory of affected deployments and retain an audit report for each. Review recorded migration history alongside actual widths and overflow counts.
2. If a formerly widened deployment is found, identify every consumer of affected genomic and non-genomic text before choosing an owned-NOTE encoding. The shared columns contain more than genomics; rewriting them to references before their readers support those references would lose usable source text.
3. Implement and test a forward, resumable backfill only for the evidenced schema/data paths. Preserve original values and dates, patient/fact/context ownership, ambiguous or missing legacy references, and historical notes. Do not manufacture clinical assertions to hold overflow.
4. Narrow only after lossless readback, reader compatibility and a final zero-overflow check are demonstrated under a strategy that handles concurrent writes. Apply deployment remediation as an explicit operational step; do not edit deployed migration history again.

Acceptance remaining: fresh and formerly widened paths converge without losing 60/61/10,000-character or non-genomics text; maximum NOTE IDs, missing/ambiguous references, retries after partial backfill and concurrent writes are handled. Current audit tests demonstrate detection and nonmutation of widened schemas; they do not claim a backfill exists.

### 2. Verify installed recipes and vocabulary

The effective registry exposes 31 components; all 73 parent/component recipes seed idempotently. Recipe v3 promotes only exact untouched variant-name seeds to LOINC 81253-7 while preserving curator decisions and local source identity. Domain resolution and audit tooling are implemented.

For each deployment, run `audit_genomics_domains` after vocabulary loading and retain its output with the vocabulary version. Resolve incomplete/ambiguous codes and table/domain mismatches through field-mapping curation, retaining source/value/unit settings. Rerun until verified. Curation changes future writes; moving existing clinical facts between tables requires a separately reviewed repair.

Retain independent transcript DNA change (48004-6), genomic DNA change (81290-9), protein change (48005-3), protein change type (48006-1), and raw variant text. Coverage depth (82121-5) currently belongs to each finding; source granularity remains to be agreed. Clone fraction has a local numeric recipe, independent of VAF. Obtain the source's actual status answer set for 69548-6. Retain chromosome 48000-4 rather than substituting the supplied 73822-9 identifier.

### 3. Finish clinical state and consumer semantics (#1240, #1246)

Effective state, contradiction rejection, inherited-component clearing, unknown placeholders, all shared dialog controls, and stale detected-summary clearing are implemented. Legacy no-call/not-tested/unrecognized assessments remain source-preserving indeterminate results. Interactive omitted units still default to percent; future imports must provide explicit units.

Remaining decisions and implementation:

- Agree marker-specific absence and no-call/not-tested/below-threshold semantics, including clone-fraction applicability. Review the complete absent-component matrix rather than assuming one gene-negative result establishes structural absence.
- Define evidence-aware TP53/del(17p) aggregation. The current legacy aggregate still returns false without qualifying evidence, including an empty record; it is not yet an evidence-aware unknown/negative contract.
- Inventory and update downstream trial/eligibility consumers against the agreed contract. Validate absent, indeterminate, no-evidence and contradictory-source cases before closing #1240.

Do not add clinical thresholds or activate complex-karyotype derivation before review. Retain existing assertions, source wording and superseded history through any approved transition.

### 4. Agree source requirements before Phase 2 (#1243)

Obtain concrete deidentified examples and decisions for:

- Discrete calls versus raw sequence files; calls per report and reports per patient; benign/reference calls; one-way ingestion versus readback.
- Explicit negatives and assay scope: gene lists, versioned panels, region/BED references or FISH probes; callable-result evidence and reporting/filtering limitations.
- Report/accession namespaces, stable identities, versions, amendments/retractions; specimen and matched-normal identifiers; mandatory clinical dates.
- Coverage granularity, interpretation versus clinical significance, origin versus genomic source class, and actual status answer sets.

Record the decisions on #1243 and link the receiving contract. Do not scaffold guessed test models or fabricate context while these requirements remain unresolved.

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

## Clinical and external coordination

Track on #1246: definitions of both complex-karyotype markers and counting/grouping by test/date; marker-specific absence; PALB1 and source example validity; disease subsets; TP53/del(17p) evidence aggregation; dated interpretation history; FISH/karyotype/clone-fraction validation. Distinguish pathogenicity, somatic evidence tier and therapeutic actionability if the source requires them.

Coordinates/alleles/external identifiers, copy number, fusion breakpoints and read counts depend on source requirements. TMB/MSI/HRD should be assay results when required. A read-only G-CDM projection remains deferred and does not replace canonical storage.

- CDEW [design PR #101](https://github.com/healthkey-ai/cdew/pull/101): coordinate the receiving contract with `vtrv101` (Vlad). Its merge/deployment is separate from PRomop delivery.
- CancerBot [PALB naming #4812](https://github.com/cancerbot-org/cancerbot/issues/4812), [example validation #5297](https://github.com/cancerbot-org/cancerbot/issues/5297) and [disease subsets #5298](https://github.com/cancerbot-org/cancerbot/issues/5298): assigned to `SamarElkassas`; preserve source strings pending reviewed answers.
- PRomop [#1229](https://github.com/healthkey-ai/promop/issues/1229): coordinate reviewed result concepts and its #1223/#1224/#1226 dependencies. Component-question curation must not flatten structured findings into answer labels.

## Verification and delivery

Use explicitly configured isolated local PostgreSQL databases for tests. Staging means Render; use its configured shell or documented staging connection for operational audits. Never inherit an unspecified remote `.env` database URL in tests.

Retain meaningful regressions for event/patient isolation, owned NOTE history, state transitions, independent components, source-preserving legacy reads, assertion/derivation separation and query budgets. For migration/backfill changes, exercise fresh and affected prior schema/data plus interrupted retries. Complete required GitHub CI before merge and update these two documents as capabilities land.

Merged verification is attached to [#1249](https://github.com/healthkey-ai/promop/pull/1249) and [#1259](https://github.com/healthkey-ai/promop/pull/1259), including full required CI. The new schema audit is covered by [PostgreSQL schema-variant tests](../tests/test_genomics_schema_audit.py): actual narrow/widened/missing/unsupported columns, character counts, read-only enforcement, rejection of row-filtered counts, repeatable-read isolation, safe retries, timeout configuration and sanitized failures. These tests do not certify live vocabulary or uninspected deployments.
