# ADR 0002 — OMOP-native therapy *types* (drug-class matching)

**Status:** Draft / Proposed (for cross-repo discussion — do not implement past Phase 0 until accepted).
**Extends:** [ADR 0001 — promop is the vocabulary source of truth](0001-vocabulary-source-of-truth.md).
**Deciders:** promop, EXACT/CB, SoC maintainers.
**Context repos:** promop (owner, vocab SoT + patient derivation), EXACT (`~/exact`, trial authoring/CB + matcher), SoC (`~/soc`).

---

## Context

Trial therapy criteria match a patient along three levels: **regimen**, **drug component**, and
**drug-class "type"** (e.g. proteasome inhibitor, IMiD, anti-CD38, BCL-2 inhibitor).

- **Regimen + component** already went OMOP-native (EXACT epic #4447; matcher flip #228,
  flag `EXACT_OMOP_THERAPY`): the patient's component **concept_ids** are pre-expanded by
  promop (`therapy_component_ids`) and matched by direct concept_id overlap; EXACT does not
  translate them.
- **Types are still CB-only.** `TherapyComponentCategory` was **intentionally NOT OMOP-mapped**
  (EXACT ADR 0001 decision A, #4502): the trial `omop_therapy_types_*` columns and their GIN
  index were **removed** (EXACT migration 0017). EXACT maps the patient's component concept_ids
  → CB category codes via a local flat table `ComponentCategoryOmopLookup`, built from CB's own
  `TherapyComponentCategoryConnection`. This is the **"CB exception"** ADR 0001 §Decision.3
  flags as scheduled for retirement.

The recorded rationale for keeping types CB-only was: *"a patient never carries a class concept,
so an `omop_therapy_types_*` column could never overlap."* That is a statement about the current
**patient payload** (component ids, no derived class ids) — **not** about OMOP lacking class
concepts. Two facts now change the calculus:

1. **OMOP class concepts exist, at the right granularity.** A curated, SME-reviewed
   category→class-concept crosswalk (the "CB therapy → OMOP concept_id" workbook) maps most CB
   categories to fine-grained HemOnc / ATC **class concepts** — see *Coverage* below.
2. **promop now stamps therapy-id provenance + release_id** ([#362](https://github.com/healthkey-ai/promop/pull/362)),
   giving the release-consistency primitive any pre-expanded, release-derived projection needs.

## Decision (proposed)

Adopt **OMOP-native therapy-type matching** via **HemOnc/ATC class-concept-id overlap** — the
same mechanism as components — for the subset of type criteria that map **losslessly** to a class
concept. Keep the rest on the CB-category path (**hybrid**, not a big-bang cutover). Specifically:

1. **Identifier space = HemOnc/ATC drug-class concept_ids** (e.g. Proteasome inhibitor `35807295`),
   NOT the 5 coarse HemOnc `Has X tx` relationship ids (they encode regimen→component *roles*,
   are far coarser than CB's ~30 category criteria, and would lose criterion meaning).
2. **Matching semantics** follow the existing type matcher: **`required` = any-overlap (OR)**,
   **`excluded` = any-hit** — *not* the regimen-level superset rule.
3. **No locally-minted concepts** (ADR 0001 §Integrity). Class concepts are licensed HemOnc/ATC.
4. **Gated by a vocabulary spike (Phase 0):** the *patient-side* `component-concept → class-concept`
   derivation must be proven against a pinned promop release before any code cutover.
5. **Fail-closed:** an unmapped/lossy `required` type criterion must **never** degrade to an empty
   requirement (which would silently drop the eligibility gate) — it stays legacy or fails closed.

## Coverage (from the SME-reviewed crosswalk)

Of ~30 CB category/class criteria:

- **~20 map to a fine-grained HemOnc/ATC class concept, SME-confirmed** — *lossless in
  concept-id space, re-authorable:* proteasome inhibitor (`35807295`), IMiD (`35807403`),
  anti-CD38 (`35807345`), anti-CD20 (`35807389`), anti-SLAMF7 (`35807363`), BCL-2 inhibitor
  (`35807456`), XPO1 inhibitor (`35807438`), CAR-T (`35807448`), ADC (`35807221`), bispecific
  antibody (`35807364`), mTOR (`35807369`), PI3K (`35807303`), HDAC (`947794`, ATC), anthracycline
  (`35807214`), alkylating agent (`35807238`), immunotherapy (`35807189`), targeted therapy
  (`912163`), monoclonal antibody (`21603754`, ATC), bisphosphonate (`35807233`), RANKL inhibitor
  (`35807351`).
- **~10 have no clean OMOP class concept — stay legacy/CB:** broad umbrellas (chemotherapy,
  hormonal_therapy, supportive_therapy, corticosteroid) and non-drug modalities (radiotherapy,
  surgery, stem_cell_transplant), plus non-therapeutic (diagnostic_tool, high-risk-smoldering).

The crosswalk covers the **trial-authoring** side (category → class concept). The **patient**
side (`component concept_id → class concept_id`) is a *different* mapping that must be derived
from promop's graph and is the subject of Phase 0.

---

## Epic — phased, cross-repo, with gates

Mirrors the component cutover (#4447 → #228). Each phase is a ticket set; **do not start a phase
before its predecessor's gate passes.**

### Phase 0 — Vocab spike (PROMOP, BLOCKING GATE, no prod change)
- **P0.1** For each of the ~20 mapped class concepts, resolve its member drugs
  (`class concept → component/ingredient concept_ids`) by traversing promop's `concept_relationship`
  and/or `concept_ancestor` on a **pinned** release. Do **not** assume `concept_ancestor` carries
  the non-hierarchical HemOnc class edges — verify.
- **P0.2** Invert to the patient direction (`component concept_id → class concept_ids`) and
  **audit precision/recall per active criterion** against EXACT's current
  `component_concept_id → CB category` lookup (`ComponentCategoryOmopLookup`), **preserving
  exclusions**.
- **P0.3** Publish the definitive **lossless vs legacy** split (subset of the ~20 that pass).
- **Gate:** no downstream phase until P0.2 meets an agreed precision/recall bar. *This is the one
  unproven dependency; if the edges don't exist on the pinned release, the cutover stalls even
  with a perfect CB mapping.*

### Phase 1 — CB / EXACT authoring (trial + mapping)
- **P1.1** Load the category→OMOP-class-concept crosswalk into the CB vocab model — add
  `TherapyComponentCategory.omop_concept_id(s)` (analogue of `Therapy.omop_concept_id` /
  `TherapyComponent.omop_concept_id`).
- **P1.2** Mark the ~10 unmapped categories **`no_omop` explicitly**, so they never resolve to
  `[]` (fail-closed guard starts here).
- **P1.3** CB owns the upstream conversion (ADR 0001 §Governance).

### Phase 2 — PROMOP (patient-side class projection + release-stamp)
- **P2.1** At refresh, derive per-line **therapy-class concept_ids** for the patient from the
  line's component concept_ids, using the P0-proven edges. Add a read-model field
  (`*_component_class_ids` / `therapy_class_ids`), **release-stamped via the [#362]
  provenance/release_id machinery** (origin + `release_id`).
- **P2.2** Expose it read-only on the patient serializer (like `*_component_ids`); add to
  `read_only_fields`.
- **Alternative (lighter):** do not pre-expand — instead **guarantee the `component→class` edges
  are in the vocab-snapshot corpus** so EXACT derives types by traversing its own release-pinned
  mirror. Same ADR 0001 tension (pre-expand vs consumer-traverse); pre-expand is symmetric with
  components and centralizes the graph walk in the SoT.

### Phase 3 — EXACT (schema + mapper + consumer + matcher)
- **P3.1 (schema, prerequisite):** restore `omop_therapy_types_required/excluded` + GIN index
  (removed in EXACT migration 0017).
- **P3.2 (mapper):** `therapy_concept_mapper.build_omop_columns` emits `omop_therapy_types_*` from
  the CB category→class-concept mapping (today explicitly skipped).
- **P3.3 (consumer):** `therapy_graph.derive_component_and_type_values` returns `type_values` as
  **class concept_ids** in OMOP mode — from the promop field (P2.1) or the mirror-traversal
  (P2.2 alternative). Legacy mode unchanged.
- **P3.4 (matcher):** overlap patient class concept_ids vs `omop_therapy_types_*` —
  `required` = any-overlap, `excluded` = any-hit — behind a **new flag `EXACT_OMOP_THERAPY_TYPES`**,
  dependent on component-id availability, dual-mode like `EXACT_OMOP_THERAPY`. One shared patient
  type-derivation used by **both** queryset and matcher.
- **P3.5 (safety):** assert **equal `release_id`** patient↔trial (ADR 0001 cross-side skew rule;
  #362 provides it). A `required` criterion mapping to `[]` must be treated fail-closed, never
  dropped.

### Phase 4 — Validation & flip
- **P4.1** Shadow-compare OMOP-type matching alongside CB-category matching on real trials/patients;
  compare **both `required` and `excluded`** outcomes; zero divergence on the covered subset.
- **P4.2** Coverage validation under the fail-closed rule.
- **P4.3** Hybrid flip per environment (like #228): OMOP types for the lossless subset, CB
  categories retained for the ~10 legacy criteria.

---

## Consequences

- **CB exception partially retired.** The type/category dimension moves to OMOP concept-id space
  for the lossless subset; the ~10 non-mappable criteria (umbrellas + non-drug modalities) keep
  the CB exception. Full retirement remains blocked "by HemOnc coverage alone" until those are
  re-authored or dropped (ADR 0001 §CB exception).
- **Hybrid matcher** (OMOP types + CB types) for the transition; a single derivation seam and one
  new flag keep it mechanically small (the profile switch already exists for components).
- **Release consistency becomes load-bearing for types too** — the patient class projection and
  the trial class columns must pin the same release; #362 is the primitive.

## Risks

1. **Phase 0 is the real gate.** If `concept_relationship`/`concept_ancestor` do not cleanly give
   `component → class` for the ~20 classes on the pinned release, the patient side can't be
   derived and the cutover stalls regardless of the CB mapping.
2. **Fail-open on partial coverage.** The current EXACT mapper drops unmapped values; for types a
   dropped `required` criterion silently removes an eligibility gate. Must be fail-closed (P1.2,
   P3.5).
3. **Granularity/semantic drift.** A CB category may be finer/coarser than its nearest class
   concept; only criteria that are provably lossless (P0.2) may flip — the rest stay legacy.

## Links

- ADR 0001 (vocab SoT), promop #362 (therapy_ids_provenance + release_id), promop #344/#359
  (`system/*.read` scope for the vocab mirror).
- EXACT: epic #4447 (component OMOP cutover), #228 (matcher flip), #4502 / EXACT-ADR-0001-decision-A
  (types kept CB-only), migration 0017 (removed `omop_therapy_types_*`), `therapy_concept_mapper.py`,
  `therapy_graph.py`, `component_category_lookup.py`, `therapy_match_profile.py`.
- Source data: the SME-reviewed "CB therapy → OMOP concept_id" crosswalk workbook.
