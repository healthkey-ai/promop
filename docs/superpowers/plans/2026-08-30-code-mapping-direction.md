# Code Mapping: source code → destination OMOP concept

**Date:** 2026-08-30
**Status:** Plan — not yet implemented
**Issue:** [#834](https://github.com/healthkey-ai/promop/issues/834) (reopened; PR #835 did not fix it)
**Executes in parallel with:** the Field Concept Mapping dialog work (separate surface, no shared files
except `patient_portal/api/views.py`, where the two feature areas are ~250 lines apart)

---

## 1. What this feature is for

Codes arrive from many places and in many states of coding:

| Channel | What the code looks like |
|---|---|
| FHIR bundle | a real LOINC / SNOMED / RxNorm / ICD-10-CM code with a `system` URI |
| Paper lab PDF | a lab's in-house test name, no code system at all |
| Doctor's note | a scribbled diagnosis, free text |
| Drug script | a drug name, sometimes an NDC or RxNorm code |

Every one of them has to end up as an **OMOP concept**. Two outcomes are possible:

1. **An Athena concept already exists** — the source code maps to a standard LOINC / SNOMED /
   RxNorm / ICD10CM / HemOnc concept.
2. **No Athena concept exists** — we mint a HealthKey concept (`source='HealthKey'`,
   `concept_id >= 2_000_000_000`, `standard_concept=NULL`) in a quarantined `HK-*` vocabulary,
   and the source code maps to that.

The Code Mapping page is the curation surface for outcome 1 and 2 alike. The direction is
always the same and never reverses:

```
   source code            →   destination OMOP concept
   (external / uncoded)       (Athena standard, or HK-* minted)

   ICD10CM  C90.00        →   concept 437233   SNOMED  Multiple myeloma
   "M-PROTEIN, SERUM"     →   concept 2000...  HK-Labs Serum M-protein   (minted)
   (no code system)
```

## 2. What is wrong today

### 2.1 The registry is inert — nothing reads it

`SourceCodeConceptMapping` (`omop_core/models.py:1793`) is written by
`patient_portal/api/views.py:8094-8213` and read by **nothing outside those views and their
tests**. FHIR ingest resolves codes in `patient_portal/api/fhir/sync.py:433 _lookup()` with a
direct `Concept` lookup on `(vocabulary_id, concept_code)` built by `_preload_concepts()`
(`sync.py:393`); an unresolved code falls to `NO_MATCHING_CONCEPT_ID = 0` with the raw text kept
in `*_source_value`.

So a curator can map `ICD10CM:C90.00 → 437233`, save it, see it listed as approved — and the
next import of that exact code still lands `concept_id=0`. This is the same failure the
`FieldConceptMapping` docstring already warns about: *"recording the decision and never acting
on it left every curated field exactly as unwritable as before."*

**This is the single most important fix in the plan.** Everything else is presentation.

### 2.2 The direction is still inverted in the data

`propose_all_code_mappings` (`views.py:8179`) walks `_local_concept_queryset()` — HealthKey
concepts in `HK-*` vocabularies — and creates a row per concept with:

```python
source_vocabulary_id=concept.vocabulary_id,   # "HK-Wearable" — a DESTINATION vocabulary
source_code=concept.concept_code,             # "HK-WEAR-STEP-LENGTH"
target_concept=concept,                       # itself
```

That is a self-mapping whose "source code system" is one of our own quarantine vocabularies. It
maps nothing. All 5 rows currently in `promop_dev` are of this shape:

```
HK-Wearable HK-WEAR-STEP-LENGTH   -> 2029606350 approved
HK-Wearable HK-WEAR-DBL-SUPPORT   -> 2029606351 approved
HK-Wearable HK-WEAR-WALK-HR       -> 2029606352 approved
HK-Wearable HK-WEAR-BASAL-ENERGY  -> 2029606353 approved
HK-Wearable HK-WEAR-HRV-RMSSD     -> 2029606354 approved
```

An `HK-*` vocabulary is where we *mint destinations*. It is never a source code system. The
same inversion is baked into the backend tests
(`patient_portal/tests.py:20711`, `:20748`, `:20773`, `:20793`) and the frontend fixtures
(`CodeMappingPage.test.tsx:18-50`), all of which use `source_vocabulary_id: "HK-Wearable"`.

### 2.3 Vestiges of #835 in the UI

| Location | Vestige |
|---|---|
| `CodeMappingPage.tsx:461` | Dialog label reads **"Source concept code"**. There is no such thing — a source code is not a concept code. It is **"Source code"**. |
| `CodeMappingPage.tsx:453` | Source code system is a free-text `<input list=…>` whose datalist is built from `row.source_vocabulary_id` values already in the table — i.e. it suggests `HK-Wearable`, propagating 2.2. |
| `CodeMappingPage.tsx:109-131` | Tabs are grouped by `source_vocabulary_id || concept_vocabulary_id`, so today they read `HK-Wearable`. |
| `CodeMappingPage.tsx:482-490` | Destination is only Concept ID + a grey name box. No destination vocabulary, no OMOP table, no concept class. |
| `CodeMappingPage.tsx:413` | The row opens the dialog only via the pencil icon in the last column. |
| `CodeMappingPage.tsx:66` | `buildEditForm` falls back `row.source_vocabulary_id \|\| row.concept_vocabulary_id` — when a mapping has no source system it silently shows the *destination* vocabulary in the source field. |
| `views.py:7997` | `_serialize_code_mapping_row` does the same fallback for `source`: `mapping.source if mapping else (concept.vocabulary_id or concept.source)`. |
| `views.py:8150` | `code_mapping_vocabularies` returns only `HK-*` vocabularies — correct for *destination*, wrong for the source-system dropdown it is currently feeding. |

### 2.4 The list is keyed by concept, not by mapping

`code_mapping_detail` is routed `code-mappings/<int:concept_id>/` (`v1_urls.py:128`) and the
frontend PATCHes `/{row.concept_id}/` carrying `mapping_id` in the body. The URL identifies the
destination, the body identifies the row. Two source codes mapping to one destination concept —
the normal case, e.g. ICD-10 and ICD-O-3 both landing on one SNOMED concept — makes the URL
ambiguous. The resource is the **mapping**, so the URL should be `code-mappings/<mapping_id>/`.

---

## 3. Target design

### 3.1 Dialog layout

```
┌── Edit Mapping ─────────────────────────────────────────────────────┐
│                                                                     │
│  SOURCE                                                             │
│  ┌──────────────────────────┬──────────────────────────┐            │
│  │ Source Code              │ Source Code System       │            │
│  │ [ C90.00              ]  │ [ ICD10CM            ▾]  │            │
│  └──────────────────────────┴──────────────────────────┘            │
│  Source Code Description  [ Multiple myeloma not having…        ]   │
│                                                                     │
│  DESTINATION                                                        │
│  ┌──────────────────────────┬──────────────────────────┐            │
│  │ Destination Concept ID   │ Destination Vocabulary   │            │
│  │ [ 437233              ]  │ [ SNOMED             ▾]  │            │
│  └──────────────────────────┴──────────────────────────┘            │
│  Destination Concept Name [ Multiple myeloma                    ]   │
│  ┌──────────────────────────┬──────────────────────────┐            │
│  │ Destination OMOP Table   │ Destination Concept Class│            │
│  │ [ Condition          ▾]  │  Clinical Finding  (ro)  │            │
│  └──────────────────────────┴──────────────────────────┘            │
│                                                                     │
│  [🔍 Search destination concepts…              ] [✨ Suggest]        │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ 437233   Multiple myeloma            SNOMED   Condition       │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  Status  [ Proposed ▾]     Notes [                              ]   │
│                                        [ Cancel ]  [ Save Mapping ] │
└─────────────────────────────────────────────────────────────────────┘
```

Field-by-field contract:

| Field | Control | Required | Source of truth |
|---|---|---|---|
| **Source Code** | text | ✅ | typed, or free text from a PDF/note |
| **Source Code System** | `<select>` of external code systems + blank | ❌ | `GET /v1/code-mappings/reference/` → `source_code_systems`. Blank is legal and means "uncoded" |
| **Source Code Description** | text | ❌ | typed; prefilled from the source vocabulary when we hold a description for the code |
| **Destination Concept ID** | number | ✅ | typed, or set by picking a concept search result |
| **Destination Vocabulary** | `<select>` of `HK-*` + blank | ❌ | only when *minting*. Blank when the destination is an existing Athena concept |
| **Destination Concept Name** | text | ✅ | read-only when the destination is an existing concept; editable when minting |
| **Destination OMOP Table** | `<select>` | ✅ | `Measurement`, `Observation`, `Condition`, `Drug Exposure`, `Procedure` — the keys of `_MAPPING_TARGETS` (`omop_core/services/write_descriptor.py:290`) |
| **Destination Concept Class** | read-only text | — | derived from the resolved concept's `concept_class_id`; blank until a concept is chosen |
| **Status** | `<select>` | ✅ | proposed / approved / rejected, defaults proposed |
| **Notes** | textarea | ❌ | |

Two things the mock-up encodes that the current dialog does not: the source block and the
destination block are visually separated with headings, and Status moves to the footer next to
the save button (it is a review action, not a source attribute — the earlier "put Status at the
top" ask in #834 was written when the dialog had no source/destination structure at all).

### 3.2 Source Code System dropdown

Served from a new reference endpoint. Contents = every `Vocabulary` row that is a plausible
*incoming* code system, i.e. **not** `HK-*` and not an OMOP-internal housekeeping vocabulary:

```python
_INTERNAL_VOCABULARIES = {'CDM', 'Episode', 'Gender', 'Race', 'Ethnicity',
                          'Type Concept', 'Visit', 'None', 'LOCAL', 'FHIR'}
```

Against `promop_dev` that yields: `ATC`, `CVX`, `HemOnc`, `ICD10CM`, `LOINC`, `RxNorm`,
`RxNorm Extension`, `SNOMED`, `UCUM`. Plus a small curated list of systems we accept codes from
but hold no concepts for, so a curator is not blocked on a vocabulary load:

```python
_EXTRA_SOURCE_CODE_SYSTEMS = [
    ('ICDO3', 'ICD-O-3 (oncology morphology/topography)'),
    ('NDC',   'National Drug Code'),
    ('CPT4',  'CPT-4 procedure codes'),
]
```

The control is a `<select>`, not a datalist over values already in the table — that is what let
`HK-Wearable` breed. It has a blank first option labelled *"— none (uncoded / free text) —"*,
because a lab PDF or a doctor's note genuinely has no code system, and #834's comment thread is
explicit that source vocabulary must not be required.

### 3.3 Tabs

One tab per **destination** `HK-*` vocabulary, from `Vocabulary.objects.filter(
vocabulary_id__startswith='HK-')` — today `HK-Drug`, `HK-Labs`, `HK-Language`, `HK-Observation`,
`HK-Regimen`, `HK-Wearable`. LOINC and SNOMED destinations get no tab: those concepts come from
Athena and are not ours to curate.

Plus two tabs that are not vocabularies:

- **All** (default) — every mapping regardless of destination.
- **Unmapped** — source codes seen at ingest that resolved to `concept_id = 0`. This is the
  actual work queue and is the reason a curator opens this page at all (see §4.6).

Tab labels carry a count badge. A vocabulary with zero mappings still gets a tab, so a curator
can see the empty `HK-Labs` bucket and start filling it.

### 3.4 Row → dialog

The whole row becomes the click target (`<tr onClick>` with `role="button"` and a keyboard
handler), keeping the pencil button for discoverability and screen readers. The approve
checkbox stops propagation so a one-click approve does not also open the dialog.

List columns, left to right, following the direction of the mapping:

| Source code | Source code system | → | Destination concept ID | Destination concept | Destination vocabulary | OMOP table | Status |

---

## 4. Work items

Each is one branch, one PR, targeting `dev`. They are ordered by dependency; 4.1–4.4 can be
worked as a single PR if that reads better in review, but 4.5 must land after 4.2.

### 4.1 — Model: separate source system from destination minting

`omop_core/models.py`, `SourceCodeConceptMapping`:

- `source_vocabulary_id` → keep the column name (it is the OMOP STCM name) but change
  `blank=True, default=''` so uncoded source codes are legal, and add a `help_text` stating it
  is an **external** code system and never an `HK-*` vocabulary.
- Add `destination_vocabulary_id = CharField(max_length=20, blank=True, default='')` — the
  `HK-*` vocabulary when the destination was minted, blank when it is an Athena concept.
- Add `omop_table = CharField(max_length=30, blank=True, default='')` with the same value
  domain as `FieldConceptMapping.omop_table`, validated through
  `write_descriptor.mapping_table_is_writable`.
- Add a `clean()` (and mirror it in the view validation) rejecting
  `source_vocabulary_id.startswith('HK-')` with the message *"HK-* vocabularies are minting
  destinations, not source code systems."* This is the guard that stops 2.2 recurring.
- The existing unique constraint `uq_sccm_source_vocabulary_code` on
  `(source_vocabulary_id, source_code)` must survive blank source systems. Postgres treats
  `''` as a value (not NULL), so `('', 'M-PROTEIN, SERUM')` and `('', 'M PROTEIN')` stay
  distinct and the constraint still holds. No change needed, but assert it in a test.

Migration `0191_source_code_mapping_direction.py`:

- schema: the two new columns, the `blank=True` on `source_vocabulary_id`.
- data: delete the self-mapping rows — `SourceCodeConceptMapping.objects.filter(
  source_vocabulary_id__startswith='HK-', source_code=F('target_concept__concept_code'))`.
  Per the CLAUDE.md data-migration rule, log each deleted row at `WARNING` with its
  `(source_vocabulary_id, source_code, target_concept_id)` before deleting, and make the
  reverse a documented no-op — the rows carried no curation decision worth reconstructing.
- Run `manage.py audit`-style dry-run first: count how many rows would be deleted on staging
  (expect 5) and confirm none of them carry a non-default `notes` or a `status` a human set.

### 4.2 — Backend: reference endpoint, mapping-keyed URLs, destination fields

`patient_portal/api/views.py`:

- `_serialize_code_mapping_row` — drop the `concept.vocabulary_id or concept.source` fallback
  for `source`; a mapping with no source system serializes as `''`, not as its destination's
  vocabulary. Add `destination_vocabulary_id`, `omop_table`, and `concept_class_id` (already
  present) to the payload, and rename the response keys to the destination-prefixed names the
  UI uses so the wire format reads in the mapping's direction:
  `destination_concept_id`, `destination_concept_name`, `destination_concept_code`,
  `destination_vocabulary_id`, `destination_concept_class_id`, `destination_omop_table`.
  Keep `concept_id` as an alias for one release — `App.test.tsx` and any external caller of
  `/v1/code-mappings/` read it today.
- New `GET /v1/code-mappings/reference/` returning
  `{source_code_systems: [...], destination_vocabularies: [...], omop_tables: [...]}`, built as
  described in §3.2 / §3.3. Keep the existing `code-mappings/vocabularies/` route as a thin
  alias for one release.
- Re-key `code_mapping_detail` to `code-mappings/<int:mapping_id>/` (`v1_urls.py:128`), so the
  URL names the resource being edited. Add `DELETE` while there — a curator who mis-keys a
  source code currently has no way to remove the row.
- `_upsert_source_code_mapping` — validate `omop_table` against
  `mapping_table_is_writable`, reject `HK-*` in `source_vocabulary_id`, and stop defaulting
  `source` to `source_vocabulary_id` (that is what wrote `HK-Wearable` into `source`).
- **Delete `propose_all_code_mappings`** and its route. It exists only to generate the
  self-mappings of 2.2; there is no correct version of "propose a mapping from a concept to
  itself". §4.6 replaces it with a proposal generator that starts from unresolved source codes,
  which is the direction that has meaning.

### 4.3 — Frontend: the dialog

`frontend/src/components/CodeMappings/CodeMappingPage.tsx`:

- `MappingForm` gains `source_code_description`, `destination_vocabulary_id`, `omop_table`;
  `target_concept_id`/`target_concept_name` are renamed `destination_concept_id`/
  `destination_concept_name` to match the wire format.
- Dialog rebuilt to the §3.1 layout with `SOURCE` / `DESTINATION` fieldset headings.
- **"Source concept code" → "Source Code"**. The word *concept* never appears on the source side.
- Source Code System and Destination Vocabulary become `<select>`s fed by
  `/v1/code-mappings/reference/`, fetched once on mount alongside the rows.
- Destination Concept Class renders as a read-only `<div>` populated from the selected concept
  (`applyConcept` already receives `domain_id`; extend `ConceptResult` with
  `concept_class_id` — `/v1/concepts/search/` already returns it).
- `buildEditForm` loses the `|| row.concept_vocabulary_id` fallback (2.3).
- Row click opens the dialog; approve checkbox calls `stopPropagation`.
- Tabs rebuilt per §3.3, driven by the reference endpoint rather than by values scraped out of
  the loaded rows.

Run `npm run lint` **and** `npm run build` before pushing — per CLAUDE.md the
`react-hooks/set-state-in-effect` rule has broken `dev` twice, and the new reference fetch on
mount is exactly the shape it fires on. Wrap it as documented.

### 4.4 — Tests for 4.1–4.3

Backend, `patient_portal/tests.py` — rewrite `SourceCodeConceptMappingTest` fixtures so the
source side is an external system (`ICD10CM:C90.00`) and the destination is a concept, not
itself. New cases:

- a mapping with a blank source code system round-trips (uncoded lab name);
- `source_vocabulary_id='HK-Labs'` is rejected with 400;
- an unknown `omop_table` is rejected with 400;
- two source codes mapping to one destination concept both persist and both are individually
  editable via `code-mappings/<mapping_id>/`;
- `DELETE` removes one mapping and leaves the sibling;
- the row payload for a mapping with no source system reports `source_vocabulary_id: ''`, not
  the destination's vocabulary.

Frontend, `CodeMappingPage.test.tsx` — fixtures re-pointed the same way. New cases: the dialog
renders the label "Source Code" and not "Source concept code"; the source-system control is a
`select` whose options come from the reference endpoint and include a blank; concept class
renders read-only after picking a search result; clicking a row body opens the dialog; clicking
the approve checkbox does not.

### 4.5 — Wire the registry into ingest (the fix that makes the page matter)

`patient_portal/api/fhir/sync.py`:

- `_preload_concepts` (`sync.py:393`) gains a second pass: after the direct
  `(vocabulary_id, concept_code)` lookups, query
  `SourceCodeConceptMapping.objects.filter(status='approved')` for the collected codes and
  overlay `(source_vocabulary_id, source_code) → target_concept` into the same cache dict.
  One extra query for the whole bundle — the cache is already built per-bundle, so this does
  **not** add per-row queries. Assert that with `CaptureQueriesContext`, matching the standard
  the bulk-write tests already hold (`CLAUDE.md` → *Bulk OMOP Row Writes*).
- `_lookup` (`sync.py:433`) resolution order, first hit wins:
  1. direct concept on `(vocab, code)` — an Athena code that resolves needs no mapping;
  2. **approved mapping on `(vocab, code)`**;
  3. **approved mapping on `('', source_text)`** — uncoded source text, matched
     case-insensitively against `source_code`, which is how a paper-lab test name or a scribbled
     diagnosis resolves;
  4. wildcard concept on code alone (existing behaviour);
  5. `NO_MATCHING_CONCEPT_ID`.

  Only `status='approved'` mappings participate. A `proposed` mapping is a draft and must not
  change what an import produces — that is the whole point of the review state.
- Precedence note worth stating in the code: a direct Athena hit beats a curated mapping. If a
  curator needs to *override* an Athena resolution, that is a different feature (a
  `SourceToConceptMap`-style override with `invalid_reason`) and is out of scope here.

**A Measurement gotcha that will bite during execution.** The four `_upsert_clinical` tables
(condition / drug / procedure / observation, `sync.py:840`) key on `(source_value, date)` and
explicitly *update the concept in place* — the docstring at `sync.py:844` says so, and
`fhir/tests.py:669` asserts it. Measurement does **not**: `_ingest_observations` keys on
`(concept_id, date, source_value-when-unmapped)` (`sync.py:536-549`), so a code that was
resolving to 0 and now resolves to 437233 finds nothing at `(437233, date)` and **inserts a
second row**, stranding the old `concept_id=0` row beside it.

So this work item has to do one of:

- **(recommended)** drop `measurement_concept_id` from the Measurement dedup key and key on
  `(measurement_source_value, date)` like the other four, matching what the bulk-write path
  already does (`CLAUDE.md` → *Bulk OMOP Row Writes*: "The concept column stays outside every
  key, which lets a vocabulary load upgrade a stored row in place instead of stranding a
  duplicate beside it"). The comment at `sync.py:532` explains the key was widened only so
  distinct *unmapped* metrics could coexist on one day — keying on source_value achieves that
  directly and is the same fix; or
- ship a one-off reconciliation command that finds `concept_id=0` measurements whose
  source_value now has an approved mapping and upgrades them in place.

Take the first. The second leaves the trap armed for the next curator.

Tests, `patient_portal/api/fhir/tests.py`:

- import a bundle whose Observation carries an unmapped LOINC code → `measurement_concept_id=0`;
  add an approved mapping; re-import → the row's concept is upgraded **in place** and the
  measurement count does not grow. This is the test that fails today and that the key change
  above is for;
- the same round trip for a Condition, which already passes via `_upsert_clinical` — keep it as
  the control;
- a `proposed` mapping does **not** change resolution;
- an uncoded source text mapping resolves a `Observation.code.text`-only resource;
- query count stays flat as the bundle grows.

### 4.6 — The Unmapped work queue

Without this the page has no inbox and a curator has to already know which code to type.

- `GET /v1/code-mappings/unmapped/` — distinct `*_source_value` across `Measurement`,
  `Observation`, `ConditionOccurrence`, `DrugExposure`, `ProcedureOccurrence` where the
  matching `*_concept_id = 0`, with an occurrence count and the OMOP table they were seen in,
  ordered by count descending. `RegimenMappingGap` (`models.py:1840`) already does exactly this
  for regimen names and should be unioned in rather than duplicated.
- Frontend: the **Unmapped** tab lists them; clicking one opens the dialog pre-filled with the
  source code, source system (where the row recorded one), and OMOP table, with the destination
  blank and the concept search pre-seeded with the source text — which is what the existing
  *Suggest* button already does, now pointed at the right string.

This replaces the deleted `propose_all_code_mappings` with a proposal path that runs in the
direction the feature actually has.

---

## 5. Sequencing against the Field Concept Mapping work

No file conflicts beyond `patient_portal/api/views.py`, where Code Mapping lives at ~8094–8213
and Field Concept Mapping at ~8250+. If both land in the same window, whichever merges second
rebases; the hunks do not overlap. `omop_core/models.py` is touched at 1793 (this work) and
3280 (that work) — likewise disjoint.

The one shared idea worth keeping consistent across both dialogs: **a curated mapping must be
acted on by the system, or the dialog is a form that files paperwork with itself.** §4.5 is this
feature's version of the write path that `build_writable_field_descriptor` is for fields.

## 6. Verification before asking to merge

Per the project's UI-round-trip rule, the acceptance evidence is not a passing unit test — it is:

1. Start the app against `promop_dev`.
2. Open Code Mapping, land on **Unmapped**, pick a real unresolved source value.
3. Map it to a destination concept, approve it.
4. Re-import the bundle that produced it.
5. Read the OMOP row back and assert `*_concept_id` is now the curated destination, not 0.

Plus both backend suites (Django runner and pytest) and `npm run lint`, `npm run build`,
`npm test -- --run` green.

## 7. Open questions

- **Overriding a wrong Athena resolution.** §4.5 gives a direct code hit precedence over a
  curated mapping. If a source system reuses a LOINC code with a different local meaning, a
  curator cannot currently correct it. Needs a decision before it bites; the OMOP-native answer
  is `source_to_concept_map` with `invalid_reason`, which we already have a table for
  (`models.py:1770`) and do not populate.
- **Minting from the dialog.** Destination Vocabulary implies "I am about to mint an `HK-*`
  concept", but nothing in this plan creates one — the curator has to already have the concept.
  Either the dialog grows a "mint new destination concept" path (concept id from
  `services/pk.py`, `source='HealthKey'`, `standard_concept=NULL`, `concept_id >= 2e9`), or the
  field is documented as "select the vocabulary of an already-minted destination". Recommend the
  former, as a follow-on issue, once §4.1–4.6 are in.
