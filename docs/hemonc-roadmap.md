# HemOnc Support Roadmap

**Status:** Planning document
**Date:** 2026-07-21
**Audience:** promop maintainers; consumers: EXACT (`~/exact`), SoC (`~/soc`), ht-phr federation host

This document lays out a prioritized roadmap for making promop's use of the HemOnc
vocabulary complete enough that downstream consumers (EXACT trial matching, SoC
standard-of-care recommendations, FHIR/mCODE interop) can rely on promop as the
**single governed source of coded therapy data**.

The governing architectural decision (ADR 0001 in both consumer repos) is:
**promop owns source→standard-vocabulary anchors** using OMOP `concept_relationship`
semantics; consumers do *no* patient-side translation. Every vocabulary gap in
promop therefore lands directly on consumers as silent eligibility/recommendation
degradation. This roadmap is ordered by which gaps hurt consumers most.

---

## 1. Current state (baseline)

### What promop already does with HemOnc

| Capability | Where | Notes |
|---|---|---|
| Full Athena HemOnc vocabulary loaded (~13.4k concepts) into `concept` / `concept_relationship` / `concept_ancestor` | `omop_core/management/commands/load_athena_vocabularies.py` | HemOnc loaded unfiltered; `concept_ancestor` restricted to HemOnc→HemOnc pairs |
| HemOnc regimen concept_ids on the read model | `PatientRecord.first_line_therapy_id`, `second_line_therapy_id`, `later_therapy_ids` (`omop_core/models.py:1747-1761`, migration 0092/0093) | Bare integers, not FKs; display text kept alongside (Option C, `docs/therapy-fields-discussion.md`) |
| LOT inference classifies drugs via HemOnc | `lot_inference_service.py::_build_hemonc_map` / `_classify_drug` | RxNorm → `Maps to` HemOnc → `concept_ancestor` drug classes; string fallback |
| Regimen episodes carry HemOnc concepts | `omop_oncology/models.py` `Episode.episode_object_concept` / `episode_source_concept` | Written by `episode_service.upsert_therapy_line_episode` |
| FHIR round-trip of HemOnc codes | Generators emit `http://ohdsi.org/omop/HemOnc` codings on regimen MedicationStatements; importer reads them (`patient_portal/api/views.py:2274-2299`) | MM/FL/BC generators; FL catalog built from live HemOnc graph |
| Generic concept API | `/api/v1/concepts/lookup/`, `/search/` (`API_SURFACE.md:396-500`) | `vocabulary_id=HemOnc` is just a filter; no regimen-specific endpoints |
| HemOnc regimen→component graph traversal | `lot_regimens.load_hemonc_regimens_for_disease()` | **Only** used by the FL FHIR generator |

### Key gaps (what HemOnc offers that promop does not use)

1. **No component expansion** — promop never answers "which drugs are in regimen R?" at
   runtime for the read model. There is no `therapy_component_ids` field. This is the
   open **promop#189** contract that EXACT's OMOP mode and SoC's `_hemonc_medication_codes`
   path are already wired to consume.
2. **Hardcoded regimen lookups drift from the vocabulary** — `lot_regimens.py`
   (`MYELOMA_REGIMEN_LOOKUP`, `REGIMEN_LOOKUP`, `*_CONCEPT_IDS`) is a hand-maintained
   frozenset map; many entries are `concept_id=None` ("not in HemOnc") even where HemOnc
   has an equivalent (e.g. VRd → RVD concept 35806260 fails exact-frozenset match).
   `ConceptSynonym` is loaded but unused for regimen alias resolution.
3. **HemOnc Context concepts unused** — `Non-curative first-line therapy`,
   `…first-line maintenance`, `…second-line`, `…subsequent-line` relationships are the
   authoritative line-of-therapy context per regimen; SoC currently hand-maintains
   `min/max_lines_of_therapy` per TreatmentOption and plans to source them from HemOnc
   (`soc/docs/soc-plan/matview-treatment-rules.md`). promop does not expose contexts at all.
4. **Outcomes are free text / 4-value SNOMED map** — per-line `*_outcome` is a string
   (CR/PR/SD/PD/VGPR); `OUTCOME_SNOMED_CODES` (`episode_service.py:50-55`) covers only 4
   values; VGPR/MRD/sCR uncoded. No RECIST categories beyond the boolean
   `measurable_disease_by_recist_status`. HemOnc/NAACCR disease-status concepts unused.
5. **Intent and discontinuation reason uncoded** — `*_intent`, `*_discontinuation_reason`
   are free-text CharFields.
6. **Treatment phases uncoded** — induction/consolidation/maintenance/bridging live as
   text inside `episode_source_value`, not as concepts.
7. **Namespace pollution** — FHIR import mints synthetic `vocabulary_id='HemOnc'`
   concepts with `FHIR-*` codes for unmatched regimen names (`views.py:2424-2470`),
   blurring licensed Athena HemOnc vs local concepts.
8. **No integrity or validation** — `*_therapy_id` are bare ints; nothing checks the id
   is a standard HemOnc Regimen concept; no biosimilar handling outside the FL generator
   (`Synth regimen of` exclusion exists only there).
9. **Lossy 3L+ representation** — `later_therapy_ids` is a flat list; line↔concept
   pairing beyond the first later line is not preserved in the read model.
10. **Frontend ignores the coded fields** — no `.tsx` consumes `*_therapy_id`; manual
    entry still produces unresolvable free text (the root cause in
    `docs/therapy-fields-discussion.md`: 6/59 distinct therapy strings matched HemOnc).

---

## 2. Consumers and interop scenarios

### EXACT (`~/exact`) — clinical-trial matching

- Fetches patients via `GET /api/patient-info/{person_id}/` (**legacy frozen endpoint**)
  with a service token (`trials/services/patient_info/ctomop_client.py`), plus direct DB
  reads for batch runs.
- Feature flag `EXACT_OMOP_THERAPY` (default OFF) switches trial-side matching to OMOP
  concept columns. Critical design rule: **EXACT does no patient-side crosswalk** — promop
  must supply pre-resolved concept_ids.
- Already wired to consume, but promop does not yet send:
  - `therapy_component_ids` (`patient_info.py:95` — explicitly cites promop#189)
  - per-line intent / discontinuation reason (silently dropped today)
  - end dates per line (washout matching is approximate)
- Regimen-identity fidelity: EXACT ADR 0001/#172 — VRd vs VRd Lite share a drug set, so
  component-set expansion cannot distinguish them; source-asserted HemOnc regimen
  concept_ids must be preserved end-to-end.
- Outcomes mapped through a lossy string `OUTCOME_MAP`; refractory status recomputed
  from text.

### SoC (`~/soc`) — standard-of-care recommendations

- Consumes promop as "CTOMOP" (same codebase; promop's Django package is literally
  `ctomop`). Contract: `docs/patient-info-payload.md` maps 1:1 onto `PatientRecord`.
- `SOC_OMOP_MEDICATIONS` (default **true**) already routes medication synthesis through
  promop's `first/second_line_therapy_id` + `later_therapy_ids`.
- `SOC_HEMONC_ARTIFACT` (default false) switches SoC from a 31-regimen hand table to a
  separately-built 159-regimen MM artifact (Athena HemOnc 2024-12-19) — a **duplicate of
  knowledge promop already has in its DB**, flagged as a clinical-safety divergence
  surface (SoC #198).
- Wants, per its payload contract and ADR 0001:
  - Full `lines_of_therapy[]`: regimen HemOnc concept_id + component RxNorm codes +
    best_response + discontinuation_reason + dates
  - HemOnc Context → auto-populated `min/max_lines_of_therapy` gating bounds
  - A versioned, promop-owned crosswalk artifact so SoC's `_hemonc_medication_codes.py`
    becomes a compiled consumer view, not a source of truth
  - Server-to-server auth (OAuth2 client_credentials against `/api/v1/patient-records/`)
    to retire the legacy-endpoint dev harness

### FHIR / mCODE interop

- Generators already emit HemOnc codings (`system=http://ohdsi.org/omop/HemOnc`); importer
  reads them. This is promop's best current interop surface and should be preserved as
  the canonical way external systems assert regimen identity.

---

## 3. Roadmap

Priority is driven by: (a) what consumers are already wired to consume, (b) clinical-safety
impact of silent mapping failures, (c) contractual commitments (promop#189, both ADR 0001s).

### P0 — Component expansion (`therapy_component_ids`)

**Unblocks:** EXACT OMOP-mode component/class matching ("no prior anti-CD38", "must have
had a proteasome inhibitor"); SoC Stage 3 medication rules for novel agents.

- Expand each line's HemOnc regimen concept → component drug concept_ids via
  `concept_relationship` (`Has cytotoxic chemo` / `Has targeted therapy` /
  `Has immunotherapy` / `Has steroid tx` / `Has hormonal tx`) at `refresh_patient_record`
  time.
- Add `therapy_component_ids` (and per-line `first/second/later_therapy_component_ids`)
  to `PatientRecord`, serializer, legacy payload, and `PatientInfo` TS type. Full
  new-attribute checklist per CLAUDE.md applies (model, migration, FHIR loader, TS, UI).
- Include drug-class ancestor expansion (HemOnc class hierarchy) or a documented
  consumer-side recipe — EXACT reverse-maps components→CB categories and needs complete
  component lists.
- Also fixes SoC's reliance on its duplicated 31-regimen table for components.

### P1 — Versioned, promop-owned crosswalk artifact

**Unblocks:** both ADR 0001 implementations; kills the three-way duplication of regimen
knowledge (promop `lot_regimens.py`, SoC `hemonc_mm_artifact.json`, EXACT
`therapy_omop_mapping.csv`).

- Management command that compiles, from the live Athena-loaded vocabulary:
  regimen concept_id → components (with relationship type) → drug-class ancestors →
  RxNorm `Maps to` anchors, plus per-regimen HemOnc Contexts.
- Version-pinned to the Athena HemOnc release date; emitted as JSON with a schema
  version; fail-closed semantics documented (`Maps to` / `Is a` / `Subsumes` / lossy /
  no-map) so consumers never silently strengthen eligibility.
- Use it to **reconcile and close known mapping gaps**: EXACT's 47
  `needs_review`/`no_omop` CSV rows; promop's `concept_id=None` entries
  (Isa-KRd, Dara-Kd, VenVD, VCd, VPd, …).
- Long-term: replace `lot_regimens.py` hardcoded dicts with this artifact (or direct
  graph queries), eliminating drift at the source.

### P2 — Structured per-line therapy history in the API

**Unblocks:** SoC payload contract (`lines_of_therapy[]`); EXACT washout precision,
intent/discontinuation matching; retires SoC's free-text parsing path
(`_medication_codes.py`) and its duplicated refractory derivation.

- Emit `lines_of_therapy[]` from `Episode`/`EpisodeEvent`/`AILineOfTherapySummary`:
  per line — HemOnc regimen concept_id (+ source-asserted vs inferred flag), component
  concept_ids, start/end dates, outcome, intent, discontinuation reason, phase.
- Preserve **source-asserted regimen identity** end-to-end (FHIR HemOnc coding →
  `episode_source_concept` → payload) so same-drug-set regimens (VRd vs VRd Lite)
  stay distinguishable — never re-derive from drug sets when source asserted.
- Serve on `/api/v1/patient-records/` only (legacy path stays frozen); add a documented
  server-to-server auth path (OAuth2 client_credentials) for SoC/EXACT backends.
- Fix 3L+ lossiness: per-line concept_ids inside `lines_of_therapy[]` supersede the
  flat `later_therapy_ids` list (kept for backwards compat).

### P3 — Regimen resolution from the live HemOnc graph

**Unblocks:** everything above at higher coverage; ends manual lookup maintenance.

- Replace exact-frozenset matching (`get_regimen_concept_id`) with graph-based
  resolution: component-set containment against HemOnc regimens + `ConceptSynonym`
  alias resolution + biosimilar exclusion (`Synth regimen of`).
- Extend HemOnc class-based drug classification beyond MM/CAR-T/steroid to FL/BC/CLL
  (`HEMONC_*_CLASSES` generalization), retiring name-string fallbacks.
- Generalize `load_hemonc_regimens_for_disease()` (currently FL-only) into a shared
  service used by MM/BC/CLL generators and by a new
  `GET /api/v1/regimens/?condition_concept_id=` endpoint (disease→indicated regimens
  via `Curr adult indic for`, with components inline).
- Stop minting `FHIR-*` pseudo-HemOnc concepts on import; quarantine unmatched regimens
  under a separate local vocabulary id and surface them in a mapping-gap report instead.

### P4 — HemOnc Contexts for line-of-therapy semantics

**Unblocks:** SoC auto-populated `min/max_lines_of_therapy`; promop phase labels as
first-class data.

- Surface HemOnc Context concepts (`Non-curative first-line therapy`, `…first-line
  maintenance`, `…second-line`, `…subsequent-line`) on regimen endpoints and in the
  crosswalk artifact (P1).
- Promote episode phase (induction/consolidation/maintenance/bridging) out of
  `episode_source_value` text into a coded field (episode modifier or Observation),
  validated against HemOnc contexts where a source assertion exists.
- Feed `therapy_lines_count` and LOT gating from the same coded source SoC's Stage 2
  already trusts.

### P5 — Coded treatment intent and discontinuation reason

**Unblocks:** EXACT criteria families that cannot be matched today ("adjuvant setting
only", "progressed on, not discontinued for toxicity").

- Add coded companions to `*_intent` and `*_discontinuation_reason`
  (`*_intent_concept_id`, `*_discontinuation_reason_concept_id`) mapped to
  SNOMED/OMOP oncology-extension concepts; keep text for display.
- Include both in the P2 `lines_of_therapy[]` payload.

### P6 — Coded treatment outcomes (deliberately lower priority)

**Unblocks:** removal of EXACT's lossy `OUTCOME_MAP`; SoC refractory rules without
free-text parsing; future outcomes-cohort work (SoC Stage 5a predicted PFS/OS).

User note: this is the "list of possible treatment outcomes" example — valuable but
**low priority** relative to P0–P5 because consumers currently tolerate the string
values and the mapping risk is bounded to the refractory/outcome criteria families.

- Define a per-disease outcome value set aligned to the disease-appropriate criteria:
  RECIST 1.1 (BC/MCL), IMWG (MM — incl. sCR/VGPR/MRD), Lugano (FL/DLBCL), iwCLL (CLL).
- Extend `OUTCOME_SNOMED_CODES` (or move to a `VocabularyLookup` + concept map) to cover
  VGPR/sCR/MRD; store `value_as_concept_id` on `LOT-{n}-outcome` Observations.
- Emit coded outcomes in the FHIR generator's `therapy-outcome` extension
  (valueCodeableConcept alongside valueString) and parse on import.

### P7 — Hardening and hygiene (continuous / as touched)

- FK or validation for `*_therapy_id` fields (must be a standard HemOnc `Regimen`
  concept); data-quality check command reporting patients whose therapy text has no
  concept resolution.
- Frontend regimen picker validated against `/api/v1/regimens/` (stops new free-text
  entropy at the source); display coded regimen names in the UI tabs.
- Full ARTEMIS compliance (TSW sequence alignment, washout windows, observation-period
  filtering) — explicitly out of scope of the current LOT-inference spec; revisit only
  if a consumer needs it.
- Migration path for EXACT/SoC off the legacy `/api/patient-info/` endpoint before its
  sunset (2026-09-01) — P2's v1 structured payload is the landing zone.

---

## 4. Priority summary

| Phase | Deliverable | Primary beneficiary | Why this rank |
|---|---|---|---|
| P0 | `therapy_component_ids` component expansion | EXACT (promop#189), SoC | Contracted; consumers already wired; highest silent-failure risk |
| P1 | Versioned crosswalk artifact | EXACT + SoC (both ADR 0001s) | Governance root; kills 3-way duplication (SoC #198 safety surface) |
| P2 | Structured `lines_of_therapy[]` on v1 API | SoC contract, EXACT washout/intent | Retires text parsing; preserves regimen identity (#172) |
| P3 | Graph-based regimen resolution + regimens API | All | Raises coverage of P0–P2; ends lookup drift |
| P4 | HemOnc Contexts → LOT semantics | SoC Stage 2 gating | Authoritative line-context data promop already stores |
| P5 | Coded intent / discontinuation | EXACT new criteria families | Data promop already captures as text |
| P6 | Coded outcomes (RECIST/IMWG/Lugano/iwCLL) | EXACT outcome map, SoC refractory | Useful but bounded blast radius; deprioritized per user |
| P7 | Hardening, UI picker, ARTEMIS, legacy sunset | All | Continuous hygiene |

## 5. Cross-references

- promop: `docs/concept-mapping.md`, `docs/therapy-fields-discussion.md`,
  `docs/superpowers/specs/2026-05-16-lot-inference-design.md`,
  `omop_core/services/lot_regimens.py`, `omop_core/services/lot_inference_service.py`,
  `omop_oncology/models.py`, `API_SURFACE.md`
- EXACT: `docs/adr/0001-cross-vocabulary-mapping.md`,
  `docs/omop/mapping/therapy_omop_mapping.csv`,
  `trials/services/patient_info/ctomop_adapter.py`, issues #172/#174/#189(promop)
- SoC: `docs/adr/0001-cross-vocabulary-mapping.md`, `docs/patient-info-payload.md`,
  `docs/soc-plan/matview-treatment-rules.md`, `core/pipeline/_hemonc_medication_codes.py`,
  `scripts/build_hemonc_artifact.py`, issues #138/#198
