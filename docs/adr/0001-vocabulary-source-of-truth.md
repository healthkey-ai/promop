# ADR 0001 — promop is the vocabulary source of truth

**Status:** Proposed (2026-07-22)
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
2. **Consumers (EXACT, SoC) pull vocabulary from promop and cache it locally.** Local
   vocabulary stores become versioned **caches** of a promop release, not sources of
   truth. Consumers do not independently vendor or curate vocabulary content.
3. **One temporary exception:** the **`cb_code ↔ concept_id`** mapping stays inside CB
   (EXACT) for the transition period only. It is `cb_code`-keyed (`TherapyOmopMapping`),
   tied to CB-specific category matching semantics, and is scheduled for retirement
   (see *CB exception* below).

## Consequences (what promop must build)

The decision is not satisfied by the existing query endpoints. promop is today a
partial vocabulary database with browse/traversal endpoints, not a distributable,
cacheable vocabulary contract. To honour the decision, promop must provide:

### Release construction & publication
- Immutable, content-addressed **release manifests**: release ID, per-vocabulary
  versions, schema version, corpus scope, build timestamp, checksums.
- **Atomic publication** (stage → validate → publish). The current
  `load_athena_vocabularies` can `TRUNCATE`-and-reload (cascading clinical tables) with
  no publish boundary; consumers must never sync an in-progress database.
- Retention of prior releases; rollback.

### Distribution surface
- **Version-addressable full snapshots** and **release-to-release deltas** with
  tombstones and replacement records.
- A **latest-release pointer** with `ETag`/`If-None-Match`; explicit **version pinning**.
- Coverage of `concept` (incl. `valid_start_date`/`valid_end_date`/`invalid_reason`),
  `concept_synonym` (no endpoint today), `concept_relationship` (incl. edge validity),
  `concept_ancestor`, `drug_strength`, `source_to_concept_map`, and vocabulary metadata.
- **Every concept/graph/lookup/search response carries the release/vocabulary version**
  (`Vocabulary.vocabulary_version` exists but is not currently returned).

### Corpus boundary
- The loader currently scopes `concept` to selected vocabularies/classes,
  `concept_relationship` to loaded-endpoint pairs, and `concept_ancestor` to
  HemOnc→HemOnc. Either **widen the corpus** to the declared "all vocabulary data" or
  **narrow this ADR's scope** explicitly. "All" must not remain aspirational.

### Integrity / no poisoning
- **No locally-minted concepts inside a licensed `vocabulary_id`.** FHIR import today
  mints `FHIR-*` rows under `vocabulary_id='HemOnc'` (before even attempting a real
  HemOnc name match). Local/unmatched content must live under a distinct local
  vocabulary id and be surfaced in a mapping-gap report. A cache must be able to trust
  that "HemOnc" means licensed Athena HemOnc.
- Validate inbound HemOnc concept_ids (vocabulary / class / standard / validity) before
  persisting them from FHIR.

### Deprecation / migration semantics
- Deprecation & replacement policy: whether consumers may auto-migrate.
- A release delta must classify each referenced id as **retained / invalidated /
  uniquely-replaced / ambiguous / unmapped**. Consumers must never blindly rewrite ids.
- Ontology changes (a relationship retired or re-validated) change cached
  expansions even when concept ids are unchanged — deltas must express this.

### Consumer conformance
- Cache freshness rules: last-known-good retention, max staleness per decision class,
  bootstrap behavior, and the **clinical fail-safe when no valid cache exists**.
- Record the cache release id + age on every match/recommendation (staleness is a
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
  **aligned** with this ADR (they become "pull + cache" rather than "call live per
  request"). The concept graph API is an interactive/reconciliation tool, **not** the
  synchronization contract — the release/snapshot/delta surface above is.
- EXACT #174 (vocabulary-bridge information loss) and SoC #198 (silent-drop, closed)
  are the safety motivations for fail-closed cache semantics.
