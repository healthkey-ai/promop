# HemOnc Support Roadmap

**Status:** Planning document — **revised 2026-07-24** (see Revision notes)
**Original date:** 2026-07-21
**Audience:** promop maintainers; consumers: EXACT (`~/exact`), SoC (`~/soc`), ht-phr federation host
**Tracking:** implementation is tracked in **[#236](https://github.com/healthkey-ai/promop/issues/236)** (the P0/P0b umbrella; the earlier #238 epic was consolidated into it). Remaining P1–P7 items are individual issues linked from #236.

This document lays out a prioritized roadmap for making promop's use of the HemOnc
vocabulary complete enough that downstream consumers (EXACT trial matching, SoC
standard-of-care recommendations, FHIR/mCODE interop) can rely on promop as the
**single governed source of coded therapy data**.

> **Governing decision (agreed 2026-07-22, see [ADR 0001](adr/0001-vocabulary-source-of-truth.md) — status: Proposed):**
> **promop is the single source of truth for ALL vocabulary data** — `concept`,
> `concept_relationship`, `concept_synonym`, `concept_ancestor`, `drug_strength`,
> `source_to_concept_map`, and vocabulary versions. The **target** is for consumer
> services (EXACT, SoC) to **pull vocabulary from promop and cache it locally**
> instead of independently vendoring or curating it — **not yet implemented**: today
> EXACT still loads its own CSV and SoC its own artifact (see §2). The **only**
> permitted temporary exception is the `cb_code ↔ concept_id` mapping inside CB
> (EXACT), consumer-side for the transition period and scheduled for retirement.
> Once ratified, this ADR supersedes the "consumers do no patient-side translation /
> each consumer keeps a vendored artifact" framing in the EXACT/SoC ADR 0001s (which
> are not yet updated).

The implication of that decision is stronger than the original framing: every
vocabulary gap in promop no longer just degrades one consumer at request time — it
degrades **every** consumer's cache. promop is now on the release/bootstrap/update
path for the whole platform's coded therapy data.

---

## Update (2026-07-24) — what has landed

A large slice of **P0b (integrity / hygiene)** and the provenance/typing work shipped to
`dev` (PR #256 for #236, migrations `0117_concept_source_...` + `0118_seed_hk_vocabularies_remap_fhir_concepts`, plus PR #260):

- ✅ **Stop minting fake HemOnc concepts** — FHIR import now validates an inbound HemOnc
  `concept_id` (`validate_hemonc_regimen`), matches a genuine regimen by name/synonym
  (`match_hemonc_regimen_by_name`), and otherwise **quarantines** the name under `HK-*`
  vocabularies (never under `HemOnc`), recording it in a new `RegimenMappingGap` model.
  The **last-resort arbitrary-Drug pick was replaced** by HK-* quarantine (a name-based
  `Drug` lookup for plain drug names still runs before quarantine). (was gap 7 / P0b)
- ✅ **`concept.source` column** — every vocabulary row is tagged licensed-vs-HK-local. (was #259)
- ✅ **`patientrecord.therapy_ids_provenance`** + the regimen/component fields are now
  **read-only** in the serializer (with `_ignored_ro` handling on PATCH). (was #248)
- ✅ **`vocabulary_version` in concept responses** (graph / search / list; opt-in on
  `lookup`). (was #240, PR #260)
- 🔜 **Concept synonym API** (`/concepts/{id}/synonyms/`, `/concepts/synonyms/?q=`) with a
  functional trigram index — in review (PR #261, #239).
- ⏭️ **Follow-up found in review:** `concepts/search/`'s `concept_name` trigram index is
  ineffective for Django `icontains` (raw-column vs `UPPER()` mismatch) — #262.

**Tracker note:** the parallel #238 epic was **closed and consolidated into #236**. The
remaining open work is the P0 release/sync-cache contract (release manifest, snapshot +
delta, ETag/version-pin, atomic publication, corpus boundary, `source_to_concept_map`) and
P1–P7 (structured `lines_of_therapy[]`, graph-based resolution, contexts, coded
intent/discontinuation, coded outcomes, ADR ratification). The consumer-side pull+cache
adoption (EXACT/SoC) and the temporary `cb_code ↔ concept_id` exception are unchanged.

---

## Revision note (2026-07-22)

This roadmap was reviewed against the actual `dev` code plus `exact@2omop` and
`soc@2omop`, their GitHub issues, and both ADR 0001s. Material corrections vs. the
2026-07-21 draft:

- **P0 (`therapy_component_ids`) has SHIPPED**, not "future work" — see #231/#189
  (commit `c94c669`). Fields, refresh-time expansion, serializer exposure, and
  tests all exist. The baseline "there is no `therapy_component_ids` field" was
  written before that merge.
- **The concept graph API has SHIPPED** (#232/#234): `/api/v1/concepts/{id}/ancestors/`,
  `/descendants/`, `/concepts/graph/` (batch).
- **The governing model changed** (source-of-truth decision above). The spine
  re-sequences accordingly: the versioned vocabulary release/sync product (old P1)
  is now the top priority; the shipped component field is a compatibility projection.
- Several stale specifics fixed (see inline `~~strikethrough~~`/notes): VRd→RVD and
  VCd are now mapped; VPd is not a lookup entry; consumer-readiness and counts were
  overstated.

---

## 1. Current state (baseline)

### What promop already does with HemOnc

| Capability | Where | Notes |
|---|---|---|
| Full Athena HemOnc vocabulary loaded (~13.4k concepts per the Athena export) into `concept` / `concept_relationship` / `concept_ancestor` | `omop_core/management/commands/load_athena_vocabularies.py` | HemOnc loaded; **`concept` is scoped to selected vocabularies/classes**, `concept_relationship` loaded only when both endpoints are present, `concept_ancestor` restricted to HemOnc→HemOnc. **This is NOT "all vocabulary data" yet** — see §3 P0(new). |
| HemOnc regimen concept_ids on the read model | `PatientRecord.first_line_therapy_id`, `second_line_therapy_id`, `later_therapy_ids` | Bare integers, not FKs; display text kept alongside |
| **Component concept_ids on the read model (SHIPPED, #231/#189)** | `PatientRecord.first_line_component_ids`, `second_line_component_ids`, `later_component_ids`, `therapy_component_ids` (`omop_core/models.py:1770-1785`), derived in `patient_record_service.py:558` (`_expand_component_ids`) at refresh | Union of regimen graph components + line DrugExposure concept_ids + `Maps to`/`Has ingredient` targets. **Mixed identifier levels, no type discriminator; these mapped clinical projection fields are read-only.** |
| LOT inference classifies drugs via HemOnc | `lot_inference_service.py::_build_hemonc_map` / `_classify_drug` | RxNorm → `Maps to` HemOnc → `concept_ancestor` drug classes; string fallback |
| Regimen episodes carry HemOnc concepts | `omop_oncology/models.py` `Episode.episode_object_concept` / `episode_source_concept` | Written by `episode_service.upsert_therapy_line_episode`; `episode_source_concept` only set when currently empty |
| FHIR round-trip of HemOnc codes | Generators emit `http://ohdsi.org/omop/HemOnc` codings; importer reads them | MM/FL/BC generators |
| Generic concept API | `/api/v1/concepts/lookup/`, `/search/`, `/` (list) | `vocabulary_id=HemOnc` is just a filter |
| **Concept graph API (SHIPPED, #232/#234)** | `/api/v1/concepts/{id}/ancestors/`, `/descendants/`, `/concepts/graph/` (batch) | Interactive traversal. Capped 1,000 results/source, 200 sources/request; **no version in response**; not a bulk export |
| HemOnc regimen→component graph traversal | `lot_regimens.load_hemonc_regimens_for_disease()` | FL FHIR generator; **refresh-time component expansion now also traverses the graph** |

### Key gaps

Verdicts below are against `dev` as of 2026-07-22.

1. ~~**No component expansion / no `therapy_component_ids` field.**~~ **DONE (#231/#189).**
   Recast as a *hardening* item — the field exists but is untyped, mixes identifier
   levels, carries no provenance, and is writable via the API. See §3.
2. **Hardcoded regimen lookups drift from the vocabulary** — `lot_regimens.py`
   (`MYELOMA_REGIMEN_CONCEPT_IDS`, `REGIMEN_CONCEPT_IDS`) is a hand-maintained
   frozenset map with `concept_id=None` entries (Isa-KRd, Dara-IRd, PAD, Dara-Kd,
   VenVD, …). ~~VRd → RVD 35806260 fails~~ (now mapped, `lot_regimens.py:189`);
   ~~VCd unmapped~~ (now mapped, `:201`). `ConceptSynonym` **is now loaded**
   (`models.py:1247`) but still unused for alias resolution.
3. **HemOnc Context concepts unused** — `Non-curative first-line therapy`,
   `…first-line maintenance`, `…second-line`, `…subsequent-line` are the
   authoritative line-of-therapy context per regimen; promop surfaces no
   line-of-therapy context projection (the raw Context concepts/edges are reachable
   only via the generic concept/graph endpoints). **TRUE.**
4. **Outcomes are free text / 4-value SNOMED map** — `OUTCOME_SNOMED_CODES`
   (`omop_core/services/episode_service.py:48`) maps only CR/PR/SD/PD; VGPR/MRD/sCR
   uncoded; other values fall back to `value_as_string`. **TRUE.**
5. **Intent and discontinuation reason uncoded** — `*_intent`,
   `*_discontinuation_reason` are free-text `CharField`s, no `_concept_id`. **TRUE.**
6. **Treatment phases uncoded** — induction/consolidation/maintenance/bridging live
   as text in `episode_source_value` (e.g. `KRd (induction)` for inferred LOTs;
   `LOT-{n}` on FHIR import). **TRUE.**
7. **Namespace pollution** — FHIR import mints synthetic `vocabulary_id='HemOnc'`
   concepts with `FHIR-*` codes (`views.py:2426`), and does so **before** trying to
   match an existing real HemOnc regimen by name (so `RVD` can become `FHIR-RVD`
   even when the genuine concept exists). **TRUE — now a blocker, not hygiene:** a
   consumer caching promop's HemOnc cannot distinguish these from licensed content.
8. **No integrity or validation** — `*_therapy_id` (and now `*_component_ids`) are
   bare ints/JSON; nothing checks they are standard HemOnc concepts; component
   fields are writable via v1 PATCH. **TRUE.**
9. **Lossy 3L+ representation** — `later_therapy_ids` is a flat list and
   `later_component_ids` is a single aggregate across all 3L+ lines; per-line
   concept↔line pairing beyond the first later line is not preserved. **TRUE.**
10. **Frontend partially consumes coded fields** — `TreatmentTab.tsx` renders
    `*_component_ids` (read-only) but not `*_therapy_id` / regimen names. **PARTIAL.**

**Gaps that the source-of-truth decision ADDS (new):**

11. **No synonym API** — `concept_synonym` is modeled and loaded but not served by
    any endpoint. A cache cannot mirror synonyms.
12. **No release/version in any response** — `Vocabulary.vocabulary_version` exists
    (`models.py:341`) but lookup/search/graph responses omit it; consumers cannot
    pin or detect drift.
13. **No bulk export / snapshot / delta / cache protocol** — only page-at-a-time
    browsing (100/page) over a mutating DB. No manifest, checksum, release pointer,
    ETag/`If-None-Match`, tombstones, or replacement records.
14. **No atomic publication boundary** — `load_athena_vocabularies` can
    `TRUNCATE`-and-reload (`:204`) with no staging/publish separation; a consumer
    can sync a torn, in-progress database.
15. **Concept/relationship state fields not served** — `valid_start_date`,
    `valid_end_date`, `invalid_reason` exist on the models but are not in API
    responses; a mirror cannot reason about validity.

---

## 2. Consumers and interop scenarios

### EXACT (`~/exact`) — clinical-trial matching

- Has a client for `GET /api/patient-info/{person_id}/` (legacy endpoint,
  `ctomop_client.py`; currently gated to local/DEBUG per `resolve.py`), plus direct DB
  reads for batch runs.
- **Active OMOP cutover in flight** (#221→#222→#223): `omop_shadow_compare` to zero
  drift, then flip `EXACT_OMOP_THERAPY` (default OFF, `settings.py:393`) on staging
  then prod. Therapy + therapy_components already flipped via `TherapyMatchProfile`.
- **Consumes components from its OWN local graph today**, not from the patient
  payload: `therapy_graph.py:38` resolves patient regimen ids to EXACT-internal
  `Therapy → TherapyComponent.omop_concept_id`. The patient-side intake (field
  `patient_info.py:93-95`, citing promop#189; getter `get_user_therapy_component_ids`
  at `:234`) is built and fail-closed-designed, but **has zero production callers** —
  it is the receiving end for the deferred component-only scenario (#224).
- **Direction (#232/#233):** stop storing HemOnc locally; pull concepts + the
  regimen→component→class graph from promop's API and cache. This is the
  source-of-truth decision applied consumer-side.
- Regimen-identity fidelity (#172): VRd vs VRd Lite share a drug set, so component-set
  expansion cannot distinguish them; source-asserted HemOnc regimen concept_ids must
  be preserved end-to-end.
- Outcomes mapped through a lossy string `OUTCOME_MAP`; refractory recomputed from text.
- `#174` (vocabulary-bridge information-loss → can flip eligibility) is still **OPEN**.

### SoC (`~/soc`) — standard-of-care recommendations

- Consumes promop as "CTOMOP". Contract: `docs/patient-info-payload.md` (an
  aspirational contract with documented gaps — **not** a 1:1 mirror of `PatientRecord`).
- `SOC_OMOP_MEDICATIONS` (default **true**) routes medication synthesis through
  `first/second_line_therapy_id` + `later_therapy_ids` (`_hemonc_medication_codes.py:186`).
- `SOC_HEMONC_ARTIFACT` (default false) uses a separately-built 159-regimen MM
  artifact (Athena HemOnc 2024-12-19). The legacy hand table has 31 entries per its
  docstring (`_hemonc_medication_codes.py:40`).
- Today the `SOC_HEMONC_ARTIFACT` path extracts component drug **names** and
  synthesizes RxNorm medications; SoC **does not consume `therapy_component_ids`** on
  any branch (SoC #207 *plans* local drug-class derivation — "no need to consume
  PROMOP flags"). Under the source-of-truth decision, `SOC_HEMONC_ARTIFACT` becomes a
  promop-sourced cache.
- SoC still has a consumer-owned clinical mapping `_DRUG_TO_RXNORM`
  (`_hemonc_medication_codes.py:95`) — a second curation surface to reconcile.
- `#198` (silent-drop / fail-closed) is **CLOSED and landed** (`_unmapped_gate.py`,
  `Verdict.UNMAPPED`). SoC #207 confirms the "promop stores but doesn't expose"
  framing is out of date; `best_response` gap closed by promop#206.

### FHIR / mCODE interop

- Generators emit HemOnc codings; importer reads them. Preserve as the canonical way
  external systems assert regimen identity — but the inbound HemOnc `concept_id` is
  trusted by id alone (not checked for vocabulary / Regimen class / standard / validity).

---

## 3. Roadmap (re-sequenced for the source-of-truth decision)

Priority is driven by: (a) promop being the release/bootstrap dependency for every
consumer's cache, (b) clinical-safety impact of silent mapping/cache failures,
(c) contractual commitments (promop#189, [ADR 0001](adr/0001-vocabulary-source-of-truth.md)).

### P0 (new) — Versioned vocabulary release + sync/cache contract  ⟵ *the spine*

**Unblocks:** the source-of-truth decision. Turns promop from "the most central
mutable database" into a distributable, cacheable vocabulary authority.

- **Immutable, addressable releases:** a release ID + per-vocabulary versions +
  schema version + scope + build timestamp + checksums, published as a manifest.
- **Bulk versioned snapshot + release-to-release deltas** with tombstones and
  replacement records. Cover `concept`, `concept_synonym`, `concept_relationship`,
  `concept_ancestor`, `drug_strength`, `source_to_concept_map` (not currently loaded),
  vocabulary metadata — including `valid_start_date`/`valid_end_date`/`invalid_reason`.
- **Serve the missing surfaces (gaps 11–15):** synonym endpoint; `vocabulary_version`
  in every concept/graph/lookup/search response; a latest-release pointer with
  ETag/`If-None-Match`; version pinning.
- **Atomic publication:** stage → validate → publish; never expose a torn
  `TRUNCATE`-in-progress DB (fix `load_athena_vocabularies` publish boundary).
- **Corpus boundary:** the loader currently scopes concepts to selected
  vocabularies/classes and ancestors to HemOnc→HemOnc. Either widen it to the
  declared "all vocabulary data" or narrow the decision's scope in the ADR — do not
  leave "all" aspirational.
- **Consumer cache protocol:** last-known-good retention, max staleness, rollback,
  bootstrap behavior, and the clinical fail-safe when no valid cache exists.

### P0b (new) — Release integrity & namespace hygiene  ⟵ *prerequisite, not hygiene*

**Unblocks:** publishing HemOnc as an authority at all.

- **Stop minting `FHIR-*` pseudo-HemOnc concepts** (gap 7); match real HemOnc by name
  first; quarantine unmatched regimens under a separate local vocabulary id and
  surface them in a mapping-gap report. A cache must never ingest fake HemOnc rows.
- **Validate inbound HemOnc concept_ids** (FHIR import) for vocabulary / Regimen
  class / standard / validity before persisting.
- **Make derived read-model fields read-only** (`*_component_ids`, `*_therapy_id`)
  and validated; today they are writable via v1 PATCH (`serializers.py:195`,
  `tests.py:939`). Attach provenance (asserted vs inferred; release id).

### P1 — Structured per-line therapy history in the API

**Unblocks:** SoC payload contract; EXACT washout precision, intent/discontinuation;
the durable clinical contract that the aggregate `later_component_ids` cannot provide.

- Emit `lines_of_therapy[]` from `Episode`/`EpisodeEvent`/`AILineOfTherapySummary`:
  per line — HemOnc regimen concept_id (+ **source-asserted vs inferred flag**),
  component concept_ids **typed by identifier level** (HemOnc component vs RxNorm
  ingredient vs exposure), start/end dates, outcome, intent, discontinuation, phase.
- Preserve source-asserted regimen identity end-to-end (never re-derive from drug
  sets when asserted). Note the `episode_source_concept`-only-if-empty write path
  (`episode_service.py:135`) can permanently block a corrected later assertion — fix.
- Serve on `/api/v1/patient-records/`; add server-to-server auth for consumer backends.
- Supersede the flat `later_therapy_ids` (kept for backwards compat).

### P2 — Regimen resolution from the live HemOnc graph

- Replace exact-frozenset matching (`get_regimen_concept_id`) with graph-based
  resolution that **returns ambiguous/unresolved rather than picking the first
  candidate**, uses the now-loaded `ConceptSynonym` for aliases (with candidate
  ranking + source-assertion precedence), and excludes biosimilars (`Synth regimen of`).
- Generalize `load_hemonc_regimens_for_disease()` into a shared service; optionally a
  `GET /api/v1/regimens/?condition_concept_id=` endpoint.
- This improves what promop *publishes* in P0/P1 and stops bad data at the source.

### P3 — HemOnc Contexts for line-of-therapy semantics

- Surface HemOnc Context concepts on regimen endpoints and in the release (P0).
- Promote episode phase out of `episode_source_value` text into a coded field.
- Note: Context describes a regimen's *use context*, not the patient's chronological
  LOT — treat as vocabulary evidence, never as a replacement for observed dates.

### P4 — Coded treatment intent and discontinuation reason

- Add `*_intent_concept_id`, `*_discontinuation_reason_concept_id` (SNOMED/OMOP
  oncology-extension); keep text for display; include in P1 `lines_of_therapy[]`.

### P5 — Coded treatment outcomes (deliberately lower priority)

- Per-disease value set: RECIST 1.1 (BC/MCL), IMWG (MM incl. sCR/VGPR/MRD), Lugano
  (FL/DLBCL), iwCLL (CLL). Extend `OUTCOME_SNOMED_CODES` (or a `VocabularyLookup` +
  concept map); emit in the FHIR `therapy-outcome` extension. Consumers currently
  tolerate the string values, so blast radius is bounded.

### P6 — Component compatibility projection (already shipped)  ⟵ *demoted*

- The shipped `therapy_component_ids` / `*_component_ids` fields remain as a
  convenience so consumers avoid duplicating expansion. **They are a projection of the
  P0 release, not the contract.** Hardening lives in P0b. A consumer with the released
  graph can derive components itself; this field cannot substitute for a cacheable
  source. Keep it typed, provenance-tagged, and read-only.

### P7 — Hardening and hygiene (continuous)

- Data-quality check reporting patients whose therapy text has no concept resolution.
- Frontend regimen picker validated against the vocabulary; display coded names.
- ARTEMIS compliance — out of scope unless a consumer needs it.
- **Migrate EXACT/SoC off legacy `/api/patient-info/` before sunset (2026-09-01)** —
  P1's v1 structured payload is the landing zone; both consumers are still on legacy.

---

## 4. Priority summary

| Phase | Deliverable | Primary beneficiary | Why this rank |
|---|---|---|---|
| **P0** | Versioned vocab release + sync/cache contract | All consumers' caches | The source-of-truth decision requires it; nothing downstream is safe without it |
| **P0b** | Release integrity + namespace hygiene + read-only/validated fields | All | Prerequisite to publishing HemOnc as authority (cache-poisoning + provenance) |
| **P1** | Structured `lines_of_therapy[]` on v1 API | SoC contract, EXACT washout/intent | Durable clinical contract; aggregate component fields can't answer per-line questions |
| **P2** | Graph-based regimen resolution (ambiguity-safe) | All | Raises coverage; ends lookup drift; feeds P0/P1 |
| **P3** | HemOnc Contexts → LOT semantics | SoC Stage 2 gating | Authoritative line-context data |
| **P4** | Coded intent / discontinuation | EXACT criteria families | Data promop already captures as text |
| **P5** | Coded outcomes (RECIST/IMWG/Lugano/iwCLL) | EXACT outcome map, SoC refractory | Bounded blast radius; deprioritized |
| **P6** | Component projection (shipped) | EXACT (deferred #224), SoC (opt) | Already done; convenience over the P0 release, not the contract |
| **P7** | Hardening, UI picker, ARTEMIS, legacy sunset | All | Continuous |

## 5. Open governance items

- **The `cb_code ↔ concept_id` CB exception is undocumented.** It is `cb_code`-keyed
  (`TherapyOmopMapping`, `exact/trials/models.py:277`), tied to CB category semantics
  (EXACT reverse-maps component concept_ids → CB categories at runtime), and therefore
  **cannot be retired by "full HemOnc coverage" alone** — the CB criteria must be
  re-authored. Document owner, exact key, version, expiry, and retirement gate.
- **Cache-model risks** (new): staleness as a clinical input (record cache release +
  age per match); cross-consumer version skew; promop as an update/bootstrap SPOF;
  unsafe bare-`concept_id` migration on Athena reload (a delta must classify each id
  retained/invalidated/replaced/ambiguous/unmapped — never rewrite blindly); ontology
  drift changing cached expansions without any patient id changing.
- **This roadmap embeds architectural decisions** (fail-closed semantics,
  source-asserted identity, namespace policy, id typing) that belong in the ADR, not a
  plan. See [ADR 0001](adr/0001-vocabulary-source-of-truth.md).

## 6. Cross-references

- promop: **[#236](https://github.com/healthkey-ai/promop/issues/236)** (implementation tracker),
  `docs/adr/0001-vocabulary-source-of-truth.md`, `docs/concept-mapping.md`,
  `docs/therapy-fields-discussion.md`,
  `omop_core/services/lot_regimens.py`, `omop_core/services/patient_record_service.py`,
  `omop_core/services/episode_service.py`, `patient_portal/api/v1_urls.py`, `API_SURFACE.md`
- EXACT: `docs/adr/0001-cross-vocabulary-mapping.md` (**superseded** by promop ADR 0001),
  `docs/omop/mapping/therapy_omop_mapping.csv`, `trials/services/omop/therapy_graph.py`,
  issues #172/#174/#224/#232/#233(exact), #189(promop)
- SoC: `docs/adr/0001-cross-vocabulary-mapping.md` (**superseded**),
  `docs/patient-info-payload.md`, `core/pipeline/_hemonc_medication_codes.py`,
  `scripts/build_hemonc_artifact.py`, issues #198/#207(soc)
