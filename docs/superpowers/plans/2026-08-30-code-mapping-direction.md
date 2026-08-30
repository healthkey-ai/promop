# Code Mapping: source code → destination OMOP concept

**Date:** 2026-08-30
**Status:** Plan — not yet implemented
**Issue:** [#834](https://github.com/healthkey-ai/promop/issues/834) (reopened; PR #835 did not fix it)
**Executes in parallel with:** the Field Concept Mapping dialog work (separate surface; the two
feature areas sit ~250 lines apart in `patient_portal/api/views.py` and ~1500 apart in
`omop_core/models.py`, so the hunks do not overlap)

---

## 1. What this feature is for

Codes arrive from many places and in many states of coding:

| Channel | What the code looks like |
|---|---|
| FHIR bundle | a real LOINC / SNOMED / RxNorm / ICD-10-CM code with a `system` URI |
| Paper lab PDF (`~/hk-labs`) | a lab's in-house test name, often no code system at all |
| Doctor's note | a scribbled diagnosis, free text |
| Drug script | a drug name, sometimes an NDC or RxNorm code |

Every one of them has to end up as an **OMOP concept**, and the direction never reverses:

```
   source code            →   destination OMOP concept
   (external / uncoded)       (Athena standard, or HK-* minted)

   ICD10CM  C90.00        →   concept 437233   SNOMED  Multiple myeloma
   "M-PROTEIN, SERUM"     →   concept 2000...  HK-Labs Serum M-protein   (minted at import)
   (no code system)
```

### 1.1 Resolution rule (governing)

This is the rule the whole feature hangs off. It settles both questions left open in the first
draft of this plan.

1. **A LOINC or SNOMED source code needs no mapping.** That is Athena's design: the Athena
   concept *is* the LOINC/SNOMED code. Resolve it directly and stop. These two never generate
   mapping rows, which is why they get no tab (§3.3).
2. **For any other source code** — ICD-10-CM, ICD-O-3, NDC, CPT-4, or no code system at all —
   an **approved** mapping wins, over everything, including a direct concept lookup. An
   approved mapping is a curator's deliberate decision and is the mechanism for correcting a
   resolution that is wrong.
3. **If no approved mapping exists for the code**, the import mints: it selects an existing
   `HK-*` concept for that source code if one is already there, otherwise it mints a new one
   (`source='HealthKey'`, `standard_concept=NULL`, `concept_id >= 2_000_000_000`) — and records
   a **proposed** `SourceCodeConceptMapping` alongside it.
4. A **proposed** mapping never overrides anything. It is a review item, and an
   admin/curator/SME approves, edits, or rejects it later in this UI.

So imports are self-feeding: they never block on a curator, they never silently drop a code,
and everything they invent lands in a queue with the evidence attached.

---

## 2. What is wrong today

### 2.1 The registry is inert — nothing reads it, and imports do not write it

`SourceCodeConceptMapping` (`omop_core/models.py:1793`) is written by
`patient_portal/api/views.py:8094-8213` and read by **nothing outside those views and their
tests**. FHIR ingest resolves codes in `patient_portal/api/fhir/sync.py:433 _lookup()` against a
cache built by `_preload_concepts()` (`sync.py:393`) from direct `(vocabulary_id, concept_code)`
matches; an unresolved code falls to `NO_MATCHING_CONCEPT_ID = 0` with the raw text kept in
`*_source_value`.

Both halves of §1.1 are therefore missing: approving a mapping changes nothing about a
re-import (rule 2), and an import that cannot resolve a code records no proposal (rule 3). This
is the same failure the `FieldConceptMapping` docstring already warns about: *"recording the
decision and never acting on it left every curated field exactly as unwritable as before."*

### 2.2 The direction is still inverted in the data

`propose_all_code_mappings` (`views.py:8179`) walks `_local_concept_queryset()` — HealthKey
concepts in `HK-*` vocabularies — and creates a row per concept with:

```python
source_vocabulary_id=concept.vocabulary_id,   # "HK-Wearable" — a DESTINATION vocabulary
source_code=concept.concept_code,             # "HK-WEAR-STEP-LENGTH"
target_concept=concept,                       # itself
```

A self-mapping whose "source code system" is one of our own minting vocabularies. It maps
nothing. All 5 rows in `promop_dev` are of that shape:

```
HK-Wearable HK-WEAR-STEP-LENGTH   -> 2029606350 approved
HK-Wearable HK-WEAR-DBL-SUPPORT   -> 2029606351 approved
HK-Wearable HK-WEAR-WALK-HR       -> 2029606352 approved
HK-Wearable HK-WEAR-BASAL-ENERGY  -> 2029606353 approved
HK-Wearable HK-WEAR-HRV-RMSSD     -> 2029606354 approved
```

An `HK-*` vocabulary is where we *mint destinations*. It is never a source code system. The
same inversion is baked into the backend tests (`patient_portal/tests.py:20711`, `:20748`,
`:20773`, `:20793`) and the frontend fixtures (`CodeMappingPage.test.tsx:18-50`), all of which
pass `source_vocabulary_id: "HK-Wearable"`.

### 2.3 Remaining #835 vestiges in the UI

| Location | Vestige |
|---|---|
| `CodeMappingPage.tsx:461` | Dialog label reads **"Source concept code"**. A source code is not a concept code. It is **"Source Code"**. |
| `CodeMappingPage.tsx:453` | Source code system is a free-text `<input list=…>` whose datalist is built from `source_vocabulary_id` values already in the table — so it suggests `HK-Wearable`, propagating 2.2. |
| `CodeMappingPage.tsx:109-131` | Tabs group by `source_vocabulary_id \|\| concept_vocabulary_id`, so today they read `HK-Wearable`. |
| `CodeMappingPage.tsx:482-490` | Destination is Concept ID plus a grey name box. No destination vocabulary, OMOP table, or concept class. |
| `CodeMappingPage.tsx:413` | The dialog opens only from the pencil icon in the last column. |
| `CodeMappingPage.tsx:66` | `buildEditForm` falls back `row.source_vocabulary_id \|\| row.concept_vocabulary_id` — a mapping with no source system silently displays its *destination* vocabulary in the source field. |
| `views.py:7997` | `_serialize_code_mapping_row` does the same fallback for `source`. |
| `views.py:8150` | `code_mapping_vocabularies` returns only `HK-*` vocabularies — right for the destination control, wrong for the source-system control it currently feeds. |

### 2.4 The list is keyed by concept, not by mapping

`code_mapping_detail` is routed `code-mappings/<int:concept_id>/` (`v1_urls.py:128`) and the
frontend PATCHes `/{row.concept_id}/` carrying `mapping_id` in the body. Two source codes
mapping to one destination — the normal case, e.g. an ICD-10 code and a free-text diagnosis both
landing on one SNOMED concept — makes that URL ambiguous. The resource is the **mapping**.

### 2.5 Three minting paths, only one of them governed

| Path | Mints | `source='HealthKey'` | Records anything for curation |
|---|---|---|---|
| `omop_core/services/regimen_resolution.py` | HK-Regimen, HK-Drug, HK-Observation, HK-Procedure | ✅ (`:218`) | ✅ `RegimenMappingGap` |
| `patient_portal/api/lab_results/sync.py:423 _preload_hk_concepts` | HK-Labs | ❌ **bug** (`:448-463`) | ❌ nothing |
| `patient_portal/api/fhir/sync.py` | — falls to concept 0 | — | ❌ nothing |

`regimen_resolution.py` is already the shape §1.1 rule 3 describes, and its module docstring
already states the governing principle: *"It must therefore be impossible for an ingest path to
fabricate rows that claim membership in a governed vocabulary."* It is called only from the
legacy `upload_fhir_bundle` in `views.py` (`:3341`, `:3388`, `:3411`, `:3586`, `:3640`, `:3720`)
— **not** from the newer `fhir/sync.py`, and not from `lab_results/sync.py`.

**Do not build a second minting mechanism.** Generalise this one.

---

## 3. Target design

### 3.1 Dialog layout

```
┌── Edit Mapping ─────────────────────────────────────────────────────┐
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
│  Minted by import 2026-08-30 from fhir-upload · 14 occurrences      │
│  Status  [ Proposed ▾]     Notes [                              ]   │
│                                        [ Cancel ]  [ Save Mapping ] │
└─────────────────────────────────────────────────────────────────────┘
```

| Field | Control | Required | Source of truth |
|---|---|---|---|
| **Source Code** | text | ✅ | typed, or carried in by an import |
| **Source Code System** | `<select>` of external code systems + blank | ❌ | `GET /v1/code-mappings/reference/`. Blank is legal and means "uncoded" |
| **Source Code Description** | text | ❌ | typed; prefilled by the import from the source text |
| **Destination Concept ID** | number | ✅ | typed, or set by picking a concept search result |
| **Destination Vocabulary** | `<select>` of `HK-*` + blank | ❌ | the `HK-*` vocabulary when the destination was minted; blank for an Athena concept |
| **Destination Concept Name** | text | ✅ | read-only for an existing Athena concept; editable for a minted `HK-*` one (renaming a HealthKey-authored concept is legitimate curation) |
| **Destination OMOP Table** | `<select>` | ✅ | keys of `_MAPPING_TARGETS` (`omop_core/services/write_descriptor.py:290`) |
| **Destination Concept Class** | read-only | — | from the resolved concept's `concept_class_id`; blank until a concept is chosen |
| **Provenance line** | read-only | — | who created the row: an import (with its source system and occurrence count) or a named curator |
| **Status** | `<select>` | ✅ | proposed / approved / rejected; imports write `proposed` |
| **Notes** | textarea | ❌ | |

Source and destination are visually separated with headings; Status sits in the footer beside
Save, because it is a review action rather than a source attribute. The provenance line is what
tells an SME whether they are reviewing a machine's guess or a colleague's decision.

### 3.2 Source Code System dropdown

A `<select>`, not a datalist over values already in the table — the datalist is what let
`HK-Wearable` breed. Contents come from a new reference endpoint: every `Vocabulary` that is a
plausible *incoming* code system, i.e. **not** `HK-*` and not OMOP housekeeping:

```python
_INTERNAL_VOCABULARIES = {'CDM', 'Episode', 'Gender', 'Race', 'Ethnicity',
                          'Type Concept', 'Visit', 'None', 'LOCAL', 'FHIR'}
```

Against `promop_dev` that yields `ATC`, `CVX`, `HemOnc`, `ICD10CM`, `LOINC`, `RxNorm`,
`RxNorm Extension`, `SNOMED`, `UCUM`, plus a curated list of systems we accept codes from but
hold no concepts for, so a curator is not blocked on a vocabulary load:

```python
_EXTRA_SOURCE_CODE_SYSTEMS = [
    ('ICDO3', 'ICD-O-3 (oncology morphology/topography)'),
    ('NDC',   'National Drug Code'),
    ('CPT4',  'CPT-4 procedure codes'),
]
```

The first option is blank, labelled *"— none (uncoded / free text) —"*: a lab PDF or a doctor's
note genuinely has no code system, and #834's thread is explicit that source vocabulary must not
be required.

LOINC and SNOMED **do** appear here — a curator can always hand-write a mapping from one. What
§1.1 rule 1 says is that imports never *generate* one for them.

### 3.3 Tabs and sections

Mirrors the Field Concept Mapping page, which already solves this: tabs pick a slice, and within
a tab two collapsible sections split review work from settled work
(`FieldMappingPage.tsx:96-104`, where `mapping.status === "approved"` is the whole test).

**Tabs** — one per `HK-*` destination vocabulary, from
`Vocabulary.objects.filter(vocabulary_id__startswith='HK-')`: today `HK-Drug`, `HK-Labs`,
`HK-Language`, `HK-Observation`, `HK-Regimen`, `HK-Wearable`, plus `HK-Procedure` once a
procedure is quarantined (`regimen_resolution.py:159 _ensure_hk_vocab` creates it on first use).
A vocabulary with zero mappings still gets a tab, so a curator can see the empty bucket. LOINC and SNOMED get no
tab, per §1.1 rule 1. There is **no "All" tab** — it answers no question a curator has.

**Sections within each tab:**

| Section | Contains | Why |
|---|---|---|
| **Unmapped** | mappings with `status='proposed'` | The destination concept is already defined — the import minted or chose it — but no curator has confirmed it. This is the review queue and the reason the page exists. Sorted by occurrence count descending, so the code seen 400 times is reviewed before the one seen once. |
| **Mapped** | mappings with `status='approved'` | Settled. Collapsed by default. |

`rejected` mappings are hidden behind a filter rather than given a section; rejecting is a way
to stop seeing a row, and re-surfacing it by default would defeat that.

Section headers carry counts. The tab badge shows the Unmapped count, because that is the number
a curator is working down.

### 3.4 Row → dialog

The whole row becomes the click target (`<tr onClick>` with `role="button"` and a keyboard
handler); the pencil button stays for discoverability and screen readers. The approve checkbox
calls `stopPropagation` so a one-click approve does not also open the dialog.

Columns, left to right, following the direction of the mapping:

| Source code | Source code system | → | Destination concept ID | Destination concept | Destination vocabulary | OMOP table | Seen | Status |

---

## 4. Work items

Each is one GitHub issue, one branch, one PR into `dev`, in dependency order.

### 4.1 — Model: separate source system from destination minting

`omop_core/models.py`, `SourceCodeConceptMapping`:

- `source_vocabulary_id` — keep the column name (it is the OMOP STCM name), add
  `blank=True, default=''` so uncoded source codes are legal, and a `help_text` stating it is an
  **external** code system, never `HK-*`.
- Add `destination_vocabulary_id = CharField(max_length=20, blank=True, default='')` — the
  `HK-*` vocabulary when minted, blank for an Athena destination.
- Add `omop_table = CharField(max_length=30, blank=True, default='')`, validated through
  `write_descriptor.mapping_table_is_writable`.
- Add provenance for §3.1's provenance line and §3.3's sort:
  `origin = CharField(choices=[('import','Import'),('curator','Curator')], default='curator')`,
  `origin_system = CharField(blank=True, default='')` (e.g. `fhir-upload`, `hk-labs`),
  `occurrence_count = IntegerField(default=0)`, `first_seen`/`last_seen` timestamps — the same
  shape `RegimenMappingGap` already carries (`models.py:1885-1887`), so the two can be unioned
  and eventually collapsed.
- `clean()` (mirrored in view validation) rejecting `source_vocabulary_id.startswith('HK-')`:
  *"HK-* vocabularies are minting destinations, not source code systems."* This is the guard
  that stops 2.2 recurring.
- The unique constraint `uq_sccm_source_vocabulary_code` on `(source_vocabulary_id, source_code)`
  survives blank source systems — Postgres treats `''` as a value, so `('', 'M-PROTEIN, SERUM')`
  and `('', 'M PROTEIN')` stay distinct. Assert it in a test rather than assuming it.

Migration `0191_source_code_mapping_direction.py`:

- schema: the new columns and the `blank=True`.
- data: delete the self-mappings — `filter(source_vocabulary_id__startswith='HK-',
  source_code=F('target_concept__concept_code'))`. Per the CLAUDE.md data-migration rule, log
  each at `WARNING` with `(source_vocabulary_id, source_code, target_concept_id)` before
  deleting, and document the reverse as a no-op — the rows carry no curation decision worth
  reconstructing.
- Dry-run against staging first and confirm the count is 5 and that none carries non-default
  `notes` or a human-set `status`.

### 4.2 — Backend: reference endpoint, mapping-keyed URLs, destination fields

`patient_portal/api/views.py`:

- `_serialize_code_mapping_row` — drop the `concept.vocabulary_id or concept.source` fallback
  (`:7997`); a mapping with no source system serializes `''`, never its destination's vocabulary.
  Add `destination_vocabulary_id`, `omop_table`, `concept_class_id`, and the provenance fields.
  Rename response keys to read in the mapping's direction — `destination_concept_id`,
  `destination_concept_name`, `destination_concept_code`, `destination_vocabulary_id`,
  `destination_concept_class_id`, `destination_omop_table` — keeping `concept_id` as an alias
  for one release, since `App.test.tsx` and any external caller read it today.
- New `GET /v1/code-mappings/reference/` →
  `{source_code_systems, destination_vocabularies, omop_tables}` per §3.2/§3.3. Keep
  `code-mappings/vocabularies/` as a thin alias for one release.
- Re-key `code_mapping_detail` to `code-mappings/<int:mapping_id>/` (`v1_urls.py:128`) and add
  `DELETE` — a curator who mis-keys a source code currently cannot remove the row.
- `_upsert_source_code_mapping` — validate `omop_table`, reject `HK-*` source systems, and stop
  defaulting `source` to `source_vocabulary_id` (that is what wrote `HK-Wearable` into `source`).
- **Delete `propose_all_code_mappings`** and its route. There is no correct version of proposing
  a concept→itself mapping; §4.5 replaces it with proposals generated at import, which is the
  direction that has meaning.

### 4.3 — Frontend: dialog, tabs, sections

`frontend/src/components/CodeMappings/CodeMappingPage.tsx`:

- `MappingForm` gains `source_code_description`, `destination_vocabulary_id`, `omop_table`;
  `target_concept_*` renamed `destination_concept_*` to match the wire format.
- Dialog rebuilt to §3.1 with `SOURCE`/`DESTINATION` headings and the provenance line.
  **"Source concept code" → "Source Code"**; the word *concept* never appears on the source side.
- Source Code System and Destination Vocabulary become `<select>`s fed by
  `/v1/code-mappings/reference/`, fetched once on mount with the rows.
- Destination Concept Class renders read-only from the selected concept — extend `ConceptResult`
  with `concept_class_id`, which `/v1/concepts/search/` already returns
  (`views.py:6988 _serialize_concept`).
- `buildEditForm` loses the `|| row.concept_vocabulary_id` fallback (`:66`).
- Tabs and Unmapped/Mapped sections per §3.3, driven by the reference endpoint rather than by
  values scraped from loaded rows. No "All" tab.
- Row click opens the dialog; approve checkbox stops propagation.

Run `npm run lint` **and** `npm run build` before pushing. Per CLAUDE.md the
`react-hooks/set-state-in-effect` rule has broken `dev` twice and the new reference fetch on
mount is exactly the shape it fires on — wrap it as documented.

### 4.4 — Tests for 4.1–4.3

Backend, `patient_portal/tests.py` — rewrite `SourceCodeConceptMappingTest` fixtures so the
source is an external system (`ICD10CM:C90.00`) and the destination is a different concept. New
cases: a blank source code system round-trips; `source_vocabulary_id='HK-Labs'` is rejected 400;
an unknown `omop_table` is rejected 400; two source codes mapping to one destination both persist
and are individually editable via `code-mappings/<mapping_id>/`; `DELETE` removes one and leaves
the sibling; a mapping with no source system serializes `source_vocabulary_id: ''`.

Frontend, `CodeMappingPage.test.tsx` — fixtures re-pointed the same way. New cases: the dialog
renders "Source Code" and not "Source concept code"; the source-system control is a `select`
with a blank option; concept class renders read-only after picking a search result; proposed
rows land in Unmapped and approved rows in Mapped; approving moves a row between sections;
clicking a row body opens the dialog and clicking the approve checkbox does not.

### 4.5 — Imports resolve, mint, and propose (§1.1 rules 2–4)

This is the item that makes the page matter. **Extend `omop_core/services/regimen_resolution.py`
rather than writing a new minting path** — §2.5. Rename it `concept_resolution.py` (it stopped
being regimen-specific when it grew observation and procedure variants) and add one entry point:

```python
def resolve_source_code(*, source_vocabulary_id, source_code, source_text,
                        omop_table, source_system):
    """Resolve an inbound source code to a destination concept per §1.1.

    1. LOINC/SNOMED source code  -> direct Athena concept, no mapping row.
    2. approved SourceCodeConceptMapping -> its destination, overriding (1).
    3. existing HK-* concept for this source code -> that concept.
    4. otherwise mint an HK-* concept and record a *proposed* mapping.

    Returns (concept, mapping_or_None).
    """
```

Rules 3 and 4 are `_get_or_create_quarantine_concept` (`:172`) as it already stands; the new
work is rules 1–2 and writing the proposed `SourceCodeConceptMapping` where
`record_mapping_gap` (`:118`) currently writes only a `RegimenMappingGap`. Write both for now —
the gap table is the regimen-specific report and has its own consumers
(`report_regimen_mapping_gaps.py`); §7 covers collapsing them later.

Then route the three ingest paths through it:

- **`patient_portal/api/fhir/sync.py`** — `_preload_concepts` (`:393`) gains a second pass
  overlaying approved mappings onto the same cache dict, and a third minting pass for codes
  still unresolved. One extra query per bundle, not per row: assert that with
  `CaptureQueriesContext`, matching the standard the bulk-write tests already hold
  (CLAUDE.md → *Bulk OMOP Row Writes*). `_lookup` (`:433`) applies the §1.1 order.
- **`patient_portal/api/lab_results/sync.py`** — `_preload_hk_concepts` (`:423`) currently mints
  HK-Labs concepts **without `source='HealthKey'`** (`:449-463`), which violates the
  concept_fixtures invariant at `concept_fixtures.py:512-529` that every `HK-*` row is
  HealthKey-sourced. Routing it through the shared helper fixes that bug and gets it proposed
  mappings for free. A data migration should backfill `source='HealthKey'` on existing
  `HK-Labs` rows — currently zero in `promop_dev`, so the fix is cheap now and expensive later.
- **legacy `upload_fhir_bundle`** (`views.py:3341` etc.) — already calls the module; its calls
  keep working and start emitting proposed mappings.

**A Measurement dedup-key trap that will bite here.** The four `_upsert_clinical` tables
(condition/drug/procedure/observation, `sync.py:840`) key on `(source_value, date)` and
explicitly *update the concept in place* — the docstring at `:844` says so and
`fhir/tests.py:669` asserts it. Measurement does not: `_ingest_observations` keys on
`(concept_id, date, source_value-when-unmapped)` (`sync.py:532-549`), so a code that resolved to
0 and now resolves to 437233 finds nothing at `(437233, date)` and **inserts a second row**,
stranding the old one. Fix it by dropping `measurement_concept_id` from the key and keying on
`(measurement_source_value, date)` like the other four — which is what the bulk-write path
already does (*"The concept column stays outside every key, which lets a vocabulary load upgrade
a stored row in place instead of stranding a duplicate beside it"*). The comment at `sync.py:532`
shows the key was widened only so distinct *unmapped* metrics could coexist on one day; keying on
source_value achieves that directly.

Tests, `patient_portal/api/fhir/tests.py` and `patient_portal/api/lab_results/tests.py`:

- an unmapped ICD-10 code mints an `HK-*` concept and a `proposed` mapping, with
  `origin='import'` and the source system recorded;
- the same code seen twice mints once and increments `occurrence_count`;
- a LOINC code resolves directly and creates **no** mapping row (rule 1);
- an **approved** mapping overrides a direct concept hit (rule 2); a **proposed** one does not
  (rule 4);
- approving a mapping and re-importing upgrades the existing Measurement **in place** and the row
  count does not grow — the test that fails today and that the key change above is for;
- the same round trip for a Condition, which already passes via `_upsert_clinical`, as a control;
- minted HK-Labs concepts carry `source='HealthKey'`;
- query count stays flat as the bundle grows.

### 4.6 — Backfill the queue from data already imported

Everything imported before 4.5 left its unresolved codes as `concept_id = 0` rows with the text
in `*_source_value` and nothing in the mapping table. A management command
`propose_mappings_from_unresolved` walks the five clinical tables for `*_concept_id = 0`, groups
by `(source_value, omop_table)`, and runs each through `resolve_source_code` so the existing
backlog appears in the Unmapped tab with real occurrence counts. `--dry-run` first; report what
it would mint before it mints.

This is what replaces the deleted `propose_all_code_mappings`, in the direction that has meaning.

---

## 5. Sequencing against the Field Concept Mapping work

No file conflicts beyond `patient_portal/api/views.py`, where Code Mapping lives at ~8094–8213
and Field Concept Mapping at ~8250+; `omop_core/models.py` is touched at 1793 here and 3280
there. Whichever merges second rebases; the hunks do not overlap.

The shared idea worth keeping consistent across both dialogs: **a curated mapping must be acted
on by the system, or the dialog is a form that files paperwork with itself.** §4.5 is this
feature's version of the write path that `build_writable_field_descriptor` is for fields.

## 6. Verification before asking to merge

Per the project's UI-round-trip rule the acceptance evidence is not a passing unit test:

1. Start the app against `promop_dev`.
2. Import a bundle carrying an ICD-10 code with no Athena match.
3. Open Code Mapping; the code is in the **Unmapped** section of its `HK-*` tab, with an
   occurrence count and an import provenance line.
4. Re-point it at a real SNOMED concept and approve it.
5. Re-import the same bundle.
6. Read the OMOP row back: `*_concept_id` is the approved destination, not the minted `HK-*`
   concept and not 0 — and there is exactly one row, not two.

Plus both backend suites (Django runner and pytest) and `npm run lint`, `npm run build`,
`npm test -- --run` green.

## 7. Open questions

- **`RegimenMappingGap` vs `SourceCodeConceptMapping`.** After §4.5 the two overlap: both record
  "an inbound name we could not match, quarantined under HK-*, awaiting curation". The gap table
  is regimen-specific and has its own reporting command. §4.5 writes both to avoid breaking that
  consumer; collapsing the gap table into the mapping table is a follow-on once the Code Mapping
  UI can show everything the regimen report shows.
- **Minting a destination from the dialog.** §1.1 puts minting at import, which is where it
  belongs. A curator facing a proposed mapping whose minted concept is wrong can re-point it at
  an Athena concept, but cannot mint a *different* HK-* concept from the dialog. Probably fine —
  flagging it in case SME review turns up a need.
