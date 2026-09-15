# PRomop Genomics: Implementation Plan

Updated **2026-09-14** for cross-machine continuation after [PR #1321](https://github.com/healthkey-ai/promop/pull/1321) merged to `dev` as `d1035ab330f3c36a343715be92d63fadaec295b3`. Both Render staging web and worker are live on that revision. The release preflight, scoped TP53 reconciliation, authenticated smoke runner and curator checklist are implemented; the remaining operational RC work is listed below. The [implemented architecture](genomics_architecture.md) describes runtime behavior; this plan owns delivery status and acceptance gates. The supplied Word documents are retained as requirements through these two canonical documents.

## Release 1.3 RC gates

The 1.3 scope is manual genomic finding entry, editing, state readback and source provenance. Reports/specimens/scope (#1243/#1244), incremental imports (#1245), expanded result mappings (#1229), and new clinical derivation rules (#1246) are deferred until after 1.3. Existing unvalidated derivations remain inactive. The schema backfill in #1237 is conditional; current active Render audits do not indicate a need for it.

**RC recommendation:** gate a 1.3 staging release candidate on **#1315, #1317, and the upgrade-readiness portion of #1316**. An issue can remain open for its production rollout after its RC criteria pass. Live production deployment is a release gate, not a prerequisite for naming a staging RC.

| Issue | Blocks 1.3 RC? | Required RC evidence and current status |
| --- | --- | --- |
| [#1315](https://github.com/healthkey-ai/promop/issues/1315) — TP53 compatibility and cache reconciliation | **Yes — pending** | PRomop code is merged in #1321. **Full staging preview, apply and repeat-preview receipts are still outstanding; no full reconciliation has run.** A read-only preview of the single dedicated synthetic record passed with no changes. EXACT #483 merged to `main`, but the Render consumers use other branches. The clean forward-port [EXACT #488](https://github.com/healthkey-ai/exact/pull/488), targeting `dev`, is open with `LeoMo42` assigned and requested as reviewer. Per owner instruction, leave that PR for him and do not deploy EXACT. Verify the RC's actual consumer includes the fix before claiming the integration gate passed. |
| [#1316](https://github.com/healthkey-ai/promop/issues/1316) — upgrade and writer readiness | **Yes — readiness portion only; final staging audit pending** | The upgrade rehearsal is complete: all 53 pending migrations applied to an isolated copy of production schema and required reference metadata, with no clinical rows copied. The combined audit passed after restoring the missing EHR actor concept 32817 locally from Athena. [Retained rehearsal evidence](https://github.com/healthkey-ai/promop/issues/1316#issuecomment-5666199241) includes the source row fingerprint and limits. **Rerun the combined preflight on the final staging candidate.** Production upgrade and vocabulary maintenance remain separate release work. |
| [#1317](https://github.com/healthkey-ai/promop/issues/1317) — final staging smoke | **Yes — pending** | Both staging services are live on merged candidate `d1035ab`. **The authenticated smoke has not yet been repeated on that candidate.** Candidate `0c655fa` passed all seven API checks previously. Retain final web/worker revisions, preflight, CRUD/state/aggregate/provenance results and synthetic artifact accounting. Samar's checklist is available; no curator results have been recorded here. Fix any release-blocking findings before accepting the RC. |
| [#1286](https://github.com/healthkey-ai/promop/issues/1286) — live production prerequisites | **No, once #1316's RC evidence passes; blocks production release** | Production still needs its migration chain, authoritative actor concept 32817 restoration through vocabulary maintenance, and a passing combined audit on the deployed revision. Preserve curator decisions and handle vocabulary publication metadata consistently. Do not close this issue based on staging or rehearsal evidence. |

The remaining open issues **do not gate 1.3 RC in their full scope**:

- **#1237:** conditional schema remediation; active Render width audits currently show no need for a backfill. New width/overflow failures would block the affected deployment.
- **#1240:** the approved TP53 true/unknown correction is gated through #1315; broader negative, contradictory-source and TP53/del(17p) clinical rules are deferred.
- **#1243, #1244 and #1245:** source agreement, reports/specimens/scope and incremental imports are after 1.3.
- **#1246:** further clinical catalog/derivation decisions are deferred; unvalidated derivations remain inactive.
- **#1229:** expanded genomic answer mappings and their shared mapping dependencies are after 1.3. Defects in existing supported manual write/readback workflows still block RC if they lose or misrepresent clinical data.

[Samar's staging checklist](curator_smoke_test.md) covers the clinician-facing genomics, mapping and related patient-record workflows. Record bugs separately from outstanding clinical-policy decisions.

Wrong patient/value/unit, lost findings, unknown becoming negative, unjustified positive results, or broken supported save/readback are RC blockers. Track concrete findings under #1317 or linked bug issues; cosmetic issues and requests for deferred capabilities do not automatically block RC.

Before the production 1.3 release, additionally complete the live-production portions of #1315/#1316 and #1286: compatible EXACT deployment, vocabulary prerequisites, application/migration rollout, combined audit and scoped reconciliation. A code merge alone does not complete an operational gate. Cloud Run verification is outside this release work.

### Next actions from another machine

Start from current `origin/dev`; #1321 is already merged. There is no remaining unmerged PRomop runtime patch needed to use these commands. Verify the intended candidate again if `dev` has advanced since this handoff.

1. **#1316 — final staging preflight.** Confirm `promop-staging` and `promop-staging-worker` use the same intended revision and Render staging database. Run in the confirmed staging service environment:

   ```bash
   python manage.py audit_genomics_release --environment render-staging --check
   ```

   Retain the complete JSON and deployed revision. The earlier passing staging audit and production-schema rehearsal do not replace this candidate receipt.

2. **#1315 — preview the full staging TP53 reconciliation.** Use the deployed command in the staging service environment; a Render one-off job can avoid laptop-to-database latency for the full population:

   ```bash
   python manage.py reconcile_tp53_cache --all
   ```

   Review before/after counts, transitions, completion and held pending edits. The historical estimate of 3,000 false-to-null changes used cached input, so it is not the required source-based preview. The 2026-09-14 16:35 UTC read-only synthetic preview processed one record (`null->null`, zero changes, zero held edits); it does not cover the rest of staging. **No one-off reconciliation job or apply run has been started.**

3. **#1315 — EXACT handoff.** [PR #488](https://github.com/healthkey-ai/exact/pull/488) contains the fix for `dev`; `LeoMo42` owns review and deployment selection. Do not merge/deploy it on his behalf under the current instruction. The inspected Render services were `exact-2` on `dev` at `41d27e8` and `exact` on `fix/legacy-trials-schema` at `2edbe62`; neither contained the explicit TP53 handling from #483. `#488` does not by itself update the legacy-schema branch. Obtain evidence for the actual RC consumer: explicit null remains unknown through normalization and required/excluded eligibility checks, and an explicit positive remains positive. CancerBot is outside this gate.

4. **#1315 — apply and verify staging reconciliation.** After reviewing the preview and coordinating compatible consumer use, run:

   ```bash
   python manage.py reconcile_tp53_cache --all --apply
   python manage.py reconcile_tp53_cache --all
   ```

   Retain both receipts with scope and deployed SHA. Require no unexplained remaining changes; account for held edits separately. Apply changes only `tp53_disruption`, preserves source facts and unrelated projections, and supports retry after partial completion. Do not substitute a blanket false-to-null update or the broader `backfill_patient_records` command. See [the TP53 rollout contract](tp53_aggregate_plan.md).

5. **#1317 — repeat the authenticated smoke against the final candidate.** The committed runner is [scripts/genomics_release_smoke.py](../scripts/genomics_release_smoke.py). Supply `GENOMICS_SMOKE_USERNAME` and `GENOMICS_SMOKE_PASSWORD` through the operator environment. Dedicated synthetic patient **7092** was provisioned with run ID **`1dd2b6775746405dbbd66c03a918c712`**; it had no active findings after the earlier smoke. Confirm it remains designated for testing, then run:

   ```bash
   python scripts/genomics_release_smoke.py \
     --person-id 7092 --run-id 1dd2b6775746405dbbd66c03a918c712
   ```

   The runner refuses an unmarked record or one with active findings. It verifies catalog, create/positive readback, absent/indeterminate/present readback, source provenance and delete/unknown readback, and cleans up its uniquely marked finding. Check web/worker revisions before and after separately; the runner does not certify deployment identity. The prior run retained 14 retired Measurements and 7 retired Observations for synthetic patient 7092; repeat runs add retired history, which must be accounted for rather than reported as completely erased test data. If the synthetic guard fails, provision another dedicated record instead of clearing an existing patient's list.

6. **#1317 — curator findings and final status.** Have Samar use [the clinician checklist](curator_smoke_test.md). Record results and link concrete bugs, distinguishing wrong/lost clinical data or broken supported workflows from cosmetic issues and deferred clinical-policy requests. Attach candidate receipts to #1315/#1316/#1317 and update this section as each criterion passes. Keep issues open where production or external-consumer work remains.

### Evidence already available

- [PRomop #1321](https://github.com/healthkey-ai/promop/pull/1321) is merged. [Final CI](https://github.com/healthkey-ai/promop/actions/runs/34869448734) passed Django, pytest (2,711 passed, 5 skipped, 4 deselected), frontend, security and the required aggregate gate. CodeQL also passed. Local targeted coverage previously passed 100 backend and 52 UI tests; the fresh migration-recorder fixture fix passed its four audit tests.
- Render staging web deployment `dep-dak2fdjtqb8s73ekd6ng` and worker deployment `dep-dak2fdjtqb8s73ekd760` were observed live on `d1035ab330f3c36a343715be92d63fadaec295b3`. These deployments followed the authorized merge automatically; no manual deployment was triggered.
- [EXACT #483](https://github.com/healthkey-ai/exact/pull/483) merged to `main` with all three backend CI groups passing. [EXACT #488](https://github.com/healthkey-ai/exact/pull/488) is the separate, open `dev` handoff; its CI/review and delivery status must be read from that PR.
- Production web and worker remain on `f8730186d54bf297e7aab9435bb12c938af9ee8f`. No production migrations, vocabulary restoration or TP53 reconciliation were applied. #1286 and the production portion of #1316 remain open; actor concept 32817 was restored only in the local rehearsal. The [rehearsal evidence](https://github.com/healthkey-ai/promop/issues/1316#issuecomment-5666199241) is retained in GitHub so continuation does not depend on another machine's temporary files.

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
