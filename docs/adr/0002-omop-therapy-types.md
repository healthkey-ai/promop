# ADR 0002 — OMOP-native therapy *types* (drug-class matching)

**Status:** **Accepted 2026-08-03** (cross-repo: promop / CancerBot / EXACT). Supersedes the
"types are not OMOP-mapped" decision (**EXACT ADR 0001 decision A / CB #4502**): its premise —
*"a patient never carries a class concept"* — no longer holds now that promop pre-expands the
patient's class concept_ids (`*_therapy_type_ids`, [#370]). Feasibility gate (Phase 0) is
**green** — the `component → class` derivation exists and is queryable; remaining validation is a
rollout-time shadow-compare, not a blocking upfront study.

**Implementation status** (epic [healthkey-ai/exact#283]): Phase 2 (promop patient projection)
**merged** — [#370]. Phase 1 (CancerBot: cancerbot#4631–4634 → PRs #4635–#4638) and Phase 3 (EXACT:
exact#284/#285 → PRs #289/#290) **in review**. Phase 4 pending: release-assert (exact#286),
shadow-compare (exact#287), hybrid flip (exact#288). The patient field was named `*_therapy_type_ids`
(not `*_component_class_ids`) to match the trial `omop_therapy_types_*` columns and the matcher's
"TYPE" wording.
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
4. **promop pre-expands the patient's type values** and ships them to consumers, symmetric with
   components. The patient's per-line **class concept_ids** are derived once in the SoT (the
   `component concept → class concept` graph walk), release-stamped, and consumed as-is by EXACT —
   EXACT does **not** traverse the vocabulary to derive types, and does **not** need the type edges
   in its snapshot mirror. This is the ADR 0001 pre-expand stance, applied to types.
5. **Fail-closed is the one hard invariant.** An unmapped/lossy `required` type criterion must
   **never** degrade to an empty requirement (which would silently drop the eligibility gate) — it
   stays legacy or fails closed. This is enforced by engineering (explicit `no_omop` marking +
   matcher guard), not by an upfront comparison study.
6. **Feasibility, not agreement, was the open question — and it is answered.** The spike proved the
   derivation exists (Phase 0, green). We do **not** gate on the new OMOP mapping matching the old
   CB lookup: the SME-curated OMOP crosswalk is the intended *replacement* for the legacy CB
   taxonomy, so divergence is not by itself a defect. Any new-vs-old comparison is a rollout-time
   **shadow-compare** (Phase 4), informational, not a blocking gate.

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

### Phase 0 — Vocab feasibility spike (PROMOP, no prod change) — **DONE, green**
- **P0.1** For each mapped class concept, resolve its member drugs
  (`class concept → component/ingredient concept_ids`) by traversing promop's `concept_relationship`
  on a **pinned** release. Do **not** assume `concept_ancestor` carries the non-hierarchical HemOnc
  class edges — verify. *(Result: the edge is `Component --[Is a]--> Component Class`; see below.)*
- **P0.2** Confirm the **corpus scope** on the pinned release includes what the patient payload
  actually contains (promop emits HemOnc Component ids → single `Is a` hop; no RxNorm bridge needed
  for those). Decide the ~2 ATC classes with no HemOnc `Is a` graph.
- **P0.3** Publish the definitive **lossless vs legacy** split (the subset that resolves).
- **What Phase 0 is NOT:** it is *not* a precision/recall study against the legacy
  `ComponentCategoryOmopLookup`. The legacy CB lookup is the thing being *replaced*; requiring the
  new mapping to reproduce it is backwards. New-vs-old comparison lives in Phase 4 (shadow-compare,
  informational). The only Phase-0 gate is **"does the derivation exist and cover the intended
  classes?"** — and it does.

#### Phase 0 — preliminary spike results (run against a local Athena/HemOnc export)

**Gate is preliminarily GREEN for the HemOnc subset.** The `component → class` edge exists and is
queryable. Key findings:

- **Mechanism = `HemOnc Component --[Is a]--> HemOnc Component Class` (transitive), NOT
  `concept_ancestor`.** The drug-class concepts are `concept_class_id='Component Class'`. A
  concept's class membership is the `Is a` edge (e.g. `Bortezomib (35802928) —Is a→ Proteasome
  inhibitor (35807295)`; multiple memberships are normal). `concept_ancestor` is the *wrong* tool
  here: a class's descendants are the ~170 **regimens** that *use* the class, not its member drugs.
- **18/18 HemOnc target classes resolve to drug members** via the transitive `Is a` closure
  (member concepts are HemOnc Component / sub-Component-Class). Drug-member counts:

  | class | concept_id | drug members (transitive) |
  |---|---|---|
  | Targeted therapy | 912163 | 277 |
  | Immunotherapy | 35807189 | 83 |
  | Alkylating agent | 35807238 | 39 |
  | Anti-CD20 | 35807389 | 16 |
  | ADC | 35807221 | 15 |
  | Bispecific antibody | 35807364 | 15 |
  | Anthracycline | 35807214 | 12 |
  | CAR-T | 35807448 | 8 |
  | PI3K inhibitor | 35807303 | 8 |
  | Anti-CD38 | 35807345 | 6 |
  | Bisphosphonate | 35807233 | 6 |
  | Proteasome inhibitor | 35807295 | 4 |
  | mTOR inhibitor | 35807369 | 4 |
  | IMiD | 35807403 | 3 |
  | Anti-SLAMF7 / BCL-2 / XPO1 / RANKL | 35807363/…456/…438/…351 | 1 each |

- **Cleaner than expected:** promop's existing `_expand_component_ids` already carries **HemOnc
  Component** concept_ids in the patient component set, so the class derivation is a **single `Is a`
  hop** from data promop already emits (no extra RxNorm bridge needed for those).
- **Two known gaps → stay legacy (or need a HemOnc equivalent):** the **2 ATC** target classes
  (`HDAC inhibitor 947794`, `Monoclonal antibody 21603754`) have **0 usable descendants** in this
  export's `concept_ancestor`; they don't participate in the HemOnc `Is a` graph. (`monoclonal
  antibody` has HemOnc sub-classes — anti-CD38/CD20 etc. — that DO resolve, so only the broad ATC
  umbrella is unmapped.)
- **Scope caveat (RxNorm bridge):** this export carries **no `HemOnc Component → Maps to → RxNorm
  Ingredient`** edges, so a patient carrying *only* RxNorm-ingredient component ids could not be
  bridged from that export alone. Not a blocker (promop emits the HemOnc Component id), but P0.2
  must confirm the corpus scope on the *pinned release* includes what the patient payload actually
  contains.

Still to close (housekeeping, not gates): confirm corpus scope on the *pinned* production release
(this run used a local Athena/HemOnc export), and decide the 2 ATC classes (keep legacy, or find a
HemOnc equivalent). Neither blocks starting the engineering.

### Phase 1 — CB / EXACT authoring (trial + mapping)
- **P1.1** Load the category→OMOP-class-concept crosswalk into the CB vocab model — add
  `TherapyComponentCategory.omop_concept_id(s)` (analogue of `Therapy.omop_concept_id` /
  `TherapyComponent.omop_concept_id`).
- **P1.2** Mark the ~10 unmapped categories **`no_omop` explicitly**, so they never resolve to
  `[]` (fail-closed guard starts here).
- **P1.3** CB owns the upstream conversion (ADR 0001 §Governance).

### Phase 2 — PROMOP (patient-side class projection + release-stamp) — **the substantive promop work**

This is where the patient's type values come from. **Decided: promop pre-expands and ships them**
(not EXACT-side mirror traversal) — symmetric with `*_component_ids`, centralizes the graph walk
in the SoT, and removes any need for EXACT to carry the type edges in its mirror. Directly
continues the #362 (provenance) / #270 (component expansion) patterns and is **self-contained**
(no CB/EXACT dependency) — buildable now.

- **P2.1** At refresh, derive per-line **therapy-type (drug-class) concept_ids** for the patient from
  the line's component concept_ids via the P0-proven `Component --[Is a]--> Component Class` closure.
  Add a read-model field (`first/second/later_therapy_type_ids` + aggregate `therapy_type_ids`),
  mirroring the `*_component_ids` shape. Reuse `_expand_component_ids`' output as the input set (it
  already carries the HemOnc Component ids → single `Is a` hop). *(Internal derivation helper stays
  `_expand_class_ids` — "class" is the OMOP Component Class concept level; "type" is the field/match
  level.)*
- **P2.2** **Release-stamp** the derivation via the [#362] provenance/release_id machinery so a
  consumer can assert patient↔trial release equality.
- **P2.3** Expose read-only on the patient serializer (like `*_component_ids`); add to
  `read_only_fields`. Cover both derivation paths (Episodes `_get_treatment_data` and inferred-LOT
  `_apply_inferred_lots`), matching the component-id test structure in
  `tests/test_therapy_component_ids.py`.

### Phase 3 — EXACT (schema + mapper + consumer + matcher)
- **P3.1 (schema, prerequisite):** restore `omop_therapy_types_required/excluded` + GIN index
  (removed in EXACT migration 0017).
- **P3.2 (mapper):** `therapy_concept_mapper.build_omop_columns` emits `omop_therapy_types_*` from
  the CB category→class-concept mapping (today explicitly skipped).
- **P3.3 (consumer):** `therapy_graph.derive_component_and_type_values` returns `type_values` as
  **class concept_ids** in OMOP mode — read **as-is from the promop patient field (P2.1)**, no local
  traversal, no mirror dependency for types. Legacy mode unchanged.
- **P3.4 (matcher):** overlap patient class concept_ids vs `omop_therapy_types_*` —
  `required` = any-overlap, `excluded` = any-hit — behind a **new flag `EXACT_OMOP_THERAPY_TYPES`**,
  dependent on component-id availability, dual-mode like `EXACT_OMOP_THERAPY`. One shared patient
  type-derivation used by **both** queryset and matcher.
- **P3.5 (safety):** assert **equal `release_id`** patient↔trial (ADR 0001 cross-side skew rule;
  #362 provides it). A `required` criterion mapping to `[]` must be treated fail-closed, never
  dropped.

### Phase 4 — Shadow-compare & flip
- **P4.1** Shadow-compare OMOP-type matching alongside CB-category matching on real trials/patients;
  log **both `required` and `excluded`** divergences. This is the *only* place new-vs-old is
  compared — and it is **informational**: divergence surfaces cases for SME review (the OMOP mapping
  may be the more-correct one), it does not by itself block the flip. **Exclusion divergences are
  the exception** — any case where OMOP matching would *weaken* an exclusion (fail-open) is a hard
  stop until resolved.
- **P4.2** Coverage validation under the fail-closed rule (no `required` criterion resolves to `[]`).
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

1. **Fail-open on partial coverage — the primary risk.** The current EXACT mapper drops unmapped
   values; for types a dropped `required` criterion silently removes an eligibility gate, and a
   dropped `excluded` value silently weakens an exclusion. Must be fail-closed (P1.2, P3.5) and is
   the one hard stop in the Phase 4 shadow-compare.
2. **Corpus-scope drift on the pinned release.** Feasibility is proven on a local export; the
   pinned production release must actually carry the `Component --Is a--> Component Class` edges for
   the classes patients present. Confirmed as P0.2 housekeeping before the promop derivation ships.
3. **Granularity/semantic drift.** A CB category may be finer/coarser than its nearest class
   concept; only the lossless subset flips — the rest stay legacy. Divergence here is surfaced (not
   gated) by the Phase 4 shadow-compare for SME adjudication.

## Links

- ADR 0001 (vocab SoT), promop #362 (therapy_ids_provenance + release_id), promop #344/#359
  (`system/*.read` scope for the vocab mirror).
- EXACT: epic #4447 (component OMOP cutover), #228 (matcher flip), #4502 / EXACT-ADR-0001-decision-A
  (types kept CB-only), migration 0017 (removed `omop_therapy_types_*`), `therapy_concept_mapper.py`,
  `therapy_graph.py`, `component_category_lookup.py`, `therapy_match_profile.py`.
- Source data: the SME-reviewed "CB therapy → OMOP concept_id" crosswalk workbook.
