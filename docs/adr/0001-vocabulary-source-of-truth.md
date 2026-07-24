# ADR 0001 — promop is the vocabulary source of truth

**Status:** Proposed (2026-07-22; access mechanism — API + cache — folded in 2026-07-24).
**Supersedes:** `exact/docs/adr/0001-cross-vocabulary-mapping.md`,
`soc/docs/adr/0001-cross-vocabulary-mapping.md` (both to be marked *Superseded* with a
link here).
**Deciders:** promop, EXACT, SoC maintainers
**Context repos:** promop (owner), EXACT (`~/exact`), SoC (`~/soc`)

---

## Context

Coded therapy/vocabulary knowledge is currently duplicated three ways:

- promop `lot_regimens.py` hand-maintained regimen→concept dicts,
- SoC `SOC_HEMONC_ARTIFACT` (`hemonc_mm_artifact.json`, built by `build_hemonc_artifact.py`),
- EXACT `trials.OmopConcept` / `trials.TherapyOmopMapping`, loaded from the vendored
  `docs/omop/mapping/therapy_omop_mapping.csv`.

Each copy is a stale-by-definition snapshot that drifts whenever the underlying Athena
vocabulary is updated. The prior consumer ADR 0001s chose "each consumer keeps a
vendored artifact; per-consumer target resolution stays consumer-side" — which
institutionalises the drift and the divergence-safety surface (SoC #198, EXACT #174).

## Decision

1. **promop is the single source of truth for ALL vocabulary data**: `concept`,
   `concept_relationship`, `concept_synonym`, `concept_ancestor`, `drug_strength`,
   `source_to_concept_map`, and vocabulary/release versions.
2. **All data clients (EXACT, SoC, and any future consumer) access promop vocabulary and
   concept-graph data exclusively through promop's HTTP API, backed by a client-side
   cache.** The cache is a transient performance/availability layer keyed on the promop
   **release id**, **not** a replicated release store or a second source of truth. No
   consumer vendors, curates, or bulk-replicates vocabulary content. (See *Access
   mechanism* below.)
3. **One temporary exception:** the **`cb_code ↔ concept_id`** mapping stays inside CB
   (EXACT) for the transition period only. It is `cb_code`-keyed (`TherapyOmopMapping`),
   tied to CB-specific category matching semantics, and is scheduled for retirement
   (see *CB exception* below).

## Access mechanism: API + cache

promop's HTTP API **is** the synchronization contract; consumers hold a transient cache, not
a snapshot. Rejected alternatives (recorded so they are not re-litigated): (a) hourly/bulk
**download** of a full `concept*` replica — contradicts "remove local copies," is
disproportionate to consumer needs, and promop exposes no bulk edge export today; (b) a
promop-published versioned **snapshot/delta artifact** — deferred; API + cache is the
committed mechanism. This decision replaces the snapshot/delta distribution mechanism the
original draft required; the concrete API guarantees are folded into *Consequences* below.

**EXACT #233 is the first consumer to conform** (informative):
- Patient side: promop is to pre-expand component concept_ids onto the patient record
  (`therapy_component_ids`, promop#189) so EXACT reads them without request-path traversal.
  **Not yet wired:** the EXACT matcher still derives components locally today, and
  `therapy_component_ids` is not yet release-stamped.
- Trial side: regimen→component expansion via the graph API at **backfill**, cached,
  release-pinned, fail-closed; stored in a **dedicated** column, never unioned into authored
  component requirements.
- Matching: the expansion is a regimen-level **OR-alternative** evaluated by **superset**
  (a complete patient therapy line ⊇ a per-regimen expansion group), not any-overlap;
  **excluded** regimens are not expanded.
- **Data-model prerequisites this exposes for promop** (resolve before EXACT Phase T ships):
  (1) `therapy_component_ids` is today an **unstamped aggregate union across all therapy
  lines** — superset on the aggregate mis-infers a regimen; promop must emit
  **line/episode-scoped component groups** with completeness and release/provenance stamping;
  (2) components are **mixed-vocabulary** (HemOnc / RxNorm / ingredient), and distinct
  regimens can share a drug *set* (e.g. VRd vs VRd Lite), so a component-set superset alone
  can equate different regimens. promop must supply **source-asserted regimen identity**, not
  only the component concept set, and the expansion output and patient components must be
  reconcilable at a common concept granularity.

## Consequences (what promop must build)

The decision is not satisfied by the existing query endpoints. promop is today a
partial vocabulary database with browse/traversal endpoints, not a versioned, cacheable
API contract. To honour the decision, promop must provide:

### Release construction & publication
- Immutable, content-addressed **release manifests**: a **release id**, per-vocabulary
  versions, schema version, corpus scope, build timestamp, checksums.
- **Atomic publication** (stage → validate → publish). The current
  `load_athena_vocabularies` can `TRUNCATE`-and-reload (cascading clinical tables) with
  no publish boundary; consumers must never read an in-progress database.
- Retention of prior releases; rollback.

### API contract (what a cache pins and validates against)
- **Every concept / graph / lookup / search response carries the immutable promop release
  id** (plus the per-vocabulary versions it draws on). Today only
  `Vocabulary.vocabulary_version` exists as a model field, surfaced on some responses (e.g.
  `include_versions` on lookup, per-node on graph, promop#240); a **release-level id, a
  current-release pointer, and explicit version pinning are NOT yet built** — they are the
  core of this work. A cache keyed on per-vocabulary version alone is unsafe: a graph can
  span vocabularies and relationship state, so cache identity must be `release_id`.
- **Version pinning**: a client can pin one release for the duration of an operation and
  detect a release change (`ETag` / `If-None-Match`, or an explicit release parameter).
- Batch traversal (graph endpoint, ≤200 sources / ≤1000 nodes per source) with an explicit
  **truncation** signal that consumers treat as fail-closed, never silent.
- API coverage of `concept` (incl. `valid_start_date`/`valid_end_date`/`invalid_reason`),
  `concept_synonym`, `concept_relationship` (incl. edge validity), `concept_ancestor`,
  `drug_strength`, `source_to_concept_map`, and vocabulary metadata — with source-asserted
  identity preserved.

### Corpus boundary
- The loader currently scopes `concept` to selected vocabularies/classes,
  `concept_relationship` to loaded-endpoint pairs, and `concept_ancestor` to
  HemOnc→HemOnc. Either **widen the corpus** to the declared "all vocabulary data" or
  **narrow this ADR's scope** explicitly. "All" must not remain aspirational.

### Integrity / no poisoning
- **No locally-minted concepts inside a licensed `vocabulary_id`.** Unmatched/local content
  (e.g. FHIR-imported drugs with no HemOnc match) must live under a distinct local
  vocabulary id and be surfaced in a mapping-gap report — never minted under
  `vocabulary_id='HemOnc'`. A cache must be able to trust that "HemOnc" means licensed
  Athena HemOnc. (Confirm the current FHIR import path already quarantines such content.)
- Validate inbound HemOnc concept_ids (vocabulary / class / standard / validity) before
  persisting them from FHIR.

### Migration semantics
- Deprecation & replacement policy: whether consumers may auto-migrate.
- Via the API, each referenced id must be classifiable as **retained / invalidated /
  uniquely-replaced / ambiguous / unmapped** across releases; consumers must never blindly
  rewrite ids.
- Ontology changes (a relationship retired or re-validated) change cached expansions even
  when concept ids are unchanged — the release id must change so caches invalidate.

### Consumer conformance
- A **shared cache**, not per-process. On Redis-less deployments (e.g. EXACT staging on
  Cloud Run) use a **DB-backed cache** so entries are shared across instances and survive
  recycles; a per-process `LocMemCache` is near-inert there.
- Cache key includes the **release id**; **pin the release per operation**, and where two
  sides are matched (e.g. patient vs trial) **assert equal release ids** — cross-side skew
  is a correctness bug, not a nuisance.
- **Fail-closed**: on cache miss + promop unavailable → `unknown`, never fail-open to
  matched; a batch/backfill fails rather than persisting a partial projection.
- **Request-scoped memoization** for repeated per-request expansions.
- Cache freshness rules: last-known-good retention, max staleness per decision class,
  bootstrap behavior, and the **clinical fail-safe when no valid cache exists**.
- Record the cache **release id + age** on every match/recommendation (staleness is a
  clinical input; cross-consumer version skew must be observable).
- Consumer conformance tests + a release compatibility policy.

## The CB exception (temporary)

- Scope: **`cb_code ↔ concept_id`** only. Not "cb_id" — the key is `cb_code`
  (`exact/trials/models.py:277`); naming it "cb_id" risks designing the wrong key.
- Stays in CB's namespace; EXACT holds only a replicated, versioned transition cache
  carrying source key/version, promop release id, mapping status/relationship semantics,
  reviewer/provenance, validity, and an explicit **owner + expiry**.
- **Retirement gate** (all must hold): every active CB criterion using the bridge has an
  approved replacement representation; direct matching against the promop release cache
  preserves exclusions, ambiguity, and source-asserted identity; zero production
  reads/writes for ≥2 published releases; a versioned archive remains for audit only.
- Caveat: because EXACT reverse-maps component concept_ids to **CB categories** at
  runtime (`therapy_graph.py`), the exception **cannot be retired by HemOnc coverage
  alone** — those criteria must be re-authored, or the exception becomes permanent.

## Governance

- This ADR is promop-owned because promop owns the data, but **release governance is
  shared**: mapping/vocabulary decisions and clinical sign-off involve promop + CB
  clinical review. A promop-only ADR without shared release ownership, reviewer
  authority, compatibility policy, and a retirement policy would just create a third
  conflicting document.
- Architectural decisions currently embedded in `docs/hemonc-roadmap.md` (fail-closed
  semantics, source-asserted identity preservation, namespace policy, id typing) should
  be lifted into this ADR.

## Status of related decisions

- EXACT #232/#233 ("don't store HemOnc locally; use promop's concept graph API") are
  **aligned** with this ADR: the concept graph / query API **is** the synchronization
  contract, consumed through a release-pinned client cache (fetch-on-demand at backfill /
  read of pre-expanded fields at request time), **not** a bulk snapshot/delta replica.
- EXACT #174 (vocabulary-bridge information loss) and SoC #198 (silent-drop, closed)
  are the safety motivations for fail-closed cache semantics.
