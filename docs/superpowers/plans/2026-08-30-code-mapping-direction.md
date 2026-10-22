# Code Mapping architecture: source code → destination OMOP concept

**Date:** 2026-08-30
**Status:** As-built architecture
**Operational contract:** [Code Mapping API](../../code-mapping-api.md)

This started as an implementation plan. The completed work and later SCCM
resolver changes make it more useful as an architecture record: it explains the
source/destination model, the curator UI, and the decisions that led there.

Sections headed “What is wrong today” and “Work items” are retained as
historical rationale. They are not a description of the running system. Where
they conflict with the operational contract, `code-mapping-api.md` wins.

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

### 1.1 Resolution rule (as built)

This is the rule the whole feature hangs off. It settles both questions left open in the first
draft of this plan.

1. **SCCM is primary for every source vocabulary.** An effective curator mapping wins over
   every fallback, including direct Athena lookup.
2. **A proposed row is unresolved.** It records an encounter and supplies curator evidence, but
   its provisional target is never a destination an importer may write.
3. **A direct Athena hit is cached in SCCM.** This applies to CPT4 as well as LOINC, SNOMED, and
   any other loaded source vocabulary. The cache entry is tagged `athena-direct`, not presented
   as a human sign-off.
4. **A direct miss invokes the suggestion service.** Its highest-ranked candidate becomes the
   target of a proposed row only.
5. **Minting is the last fallback.** An HK-* quarantine concept is created only when neither a
   direct match nor a suitable suggestion exists; its mapping remains proposed until curation.

The full external importer contract, including response shapes and Seen semantics, is maintained
in [Code Mapping API](../../code-mapping-api.md), rather than duplicated here.

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

Two blocks, visually separated, read top to bottom in the direction of the mapping.
**Every field is labelled and carries a tooltip** — the dialog currently has an unlabelled input
holding the source code value, which is indefensible on a screen this conceptually dense.

```
┌── Edit Mapping ─────────────────────────────────────────────────────────────┐
│                                                                             │
│ ╭─ SOURCE — the code as it arrived ─────────────────────────────────────╮   │
│ │ Domain              ⓘ  [ Measurement                              ▾]  │   │
│ │ Source Code System  ⓘ  [ None — uncoded / free text               ▾]  │   │
│ │ Source Code Value   ⓘ  [ M-PROTEIN, SERUM                          ]  │   │
│ │ Source Description  ⓘ  [ Serum M-protein, electrophoresis          ]  │   │
│ │ Source Concept ID   ⓘ  [ —                       ] (read-only)        │   │
│ ╰───────────────────────────────────────────────────────────────────────╯   │
│                                                                             │
│ ╭─ DESTINATION — the OMOP concept it means ─────────────────────────────╮   │
│ │ [🔍 Search LOINC concepts…                         ] [✨ Suggest]     │   │
│ │ ┌───────────────────────────────────────────────────────────────────┐ │   │
│ │ │ 33358-3  Protein.monoclonal [Mass/volume] in Serum   LOINC        │ │   │
│ │ └───────────────────────────────────────────────────────────────────┘ │   │
│ │ Destination Concept ID     ⓘ [ 3046299                             ]  │   │
│ │ Destination Concept Name   ⓘ [ Protein.monoclonal [Mass/volume]…   ]  │   │
│ │ Destination Concept Code   ⓘ [ 33358-3            ] (read-only)       │   │
│ │ Destination Vocabulary ID  ⓘ [ LOINC              ] (read-only)       │   │
│ │ Destination Concept Class  ⓘ [ Lab Test           ] (read-only)       │   │
│ │ Standard Concept           ⓘ [ S                  ] (read-only)       │   │
│ │ Destination Table          ⓘ [ measurement        ] (from Domain)     │   │
│ ╰───────────────────────────────────────────────────────────────────────╯   │
│                                                                             │
│  Proposed by import (fhir-sync) · seen 14 times                             │
│  Status ⓘ [ Proposed ▾]   Notes [                    ]  [Cancel] [Save]     │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### Source block

| Field | Control | Required | Tooltip |
|---|---|---|---|
| **Domain** | `<select>`: Condition, Drug, Measurement, Observation, Procedure | ✅ | "What kind of fact this is. Chosen first: it decides which code systems are offered and which OMOP table the fact lands in." |
| **Source Code System** | `<select>`, scoped to Domain, blank first | ❌ | "The external code system the value arrived in — NDC or ATC for drugs, ICD-10-CM or SNOMED for conditions. Leave as None for uncoded data; a parsed paper lab or a phrase from a note has no code system, which is normal." |
| **Source Code Value** | text | ✅ | "Exactly what appears in the source data — the code if there is one, otherwise the raw text." |
| **Source Description** | text | ❌ | "Human-readable description of the source code, where the source supplies one." |
| **Source Concept ID** | read-only | — | "The OMOP concept for the source code itself, if that vocabulary is loaded. Blank is normal — most source systems are ones we receive codes in without holding their concepts." |

**Domain is the first choice** because it is the one a curator can make by looking at the data,
and it settles the two things they would otherwise have to reason about separately: which code
systems are plausible, and which OMOP table the fact belongs in. Changing Domain re-scopes the
Source Code System list and re-derives the Destination Table.

`Source Concept ID` is new and read-only. It records that the source code *itself* resolves to a
loaded Athena concept — an ICD-10-CM code has a concept even when it is not the standard one the
fact should carry. Keeping it distinct from the destination is what stops the two being conflated,
which is the confusion this whole issue is about.

#### Source code systems by domain

Catalogue lives in `omop_core/services/source_vocabularies.py`, static rather than read from the
`vocabulary` table. Most of these are systems we *receive* codes in without holding their
concepts — an NDC on a dispensing record, a dm+d code from a UK extract — and deriving the list
from loaded vocabularies would offer only the handful we happen to have and block a curator from
recording a mapping they can already make correctly. OHDSI `vocabulary_id` spellings, so a
mapping recorded today lines up with a later vocabulary load.

| Domain | Systems offered |
|---|---|
| **Condition** | SNOMED, ICD10CM, ICD10, ICD10GM, ICD10CA, ICD11, ICD9CM, Read, CTV3, ICDO3, Orphanet, OMIM, HPO, MedDRA, NCIt, ICPC, CIEL, Nebraska Lexicon, DRG, APR-DRG |
| **Procedure** | SNOMED, CPT4, HCPCS, ICD10PCS, ICD9Proc, CDT, Revenue Code, OPCS4, OPS, CCAM, CCI |
| **Drug** | RxNorm, RxNorm Extension, NDC, ATC, dm+d, CVX, MVX, HemOnc, Multum, FDB, Medi-Span, Gold Standard, GPI, VANDF, NDFRT, UNII, SPL, AMT, CCDD |
| **Measurement** | LOINC, SNOMED, CPT4, UCUM, Nebraska Lexicon |
| **Observation** | SNOMED, LOINC, ICD10CM, HCPCS, NCIt, PPI |

Every domain also offers **None — uncoded / free text**, first in the list.

#### Destination block

Search sits at the top, because picking a concept fills everything below it. The fields then read
in the order a curator checks them.

| Field | Control | Required | Tooltip |
|---|---|---|---|
| **Destination Concept ID** | number, writable | ✅ | "The OMOP concept this source code means. Type an id directly or pick one from the search above." |
| **Destination Concept Name** | text | ✅ | "Name of the destination concept. Editable only for a HealthKey-minted concept; Athena concepts are named by Athena." |
| **Destination Concept Code** | read-only | — | "The destination concept's own code in its vocabulary, e.g. 33358-3." |
| **Destination Vocabulary ID** | read-only | — | "Vocabulary the destination concept belongs to — SNOMED, LOINC, or an HK-* vocabulary when we minted it." |
| **Search vocabulary** | `<select>`, defaults to the destination's, widenable to "All vocabularies" | ❌ | Scopes the concept search only. |
| **Destination Concept Class** | read-only | — | "The concept's class within its vocabulary, e.g. Clinical Finding, Lab Test." |
| **Standard Concept** | read-only | — | "'S' means a standard Athena concept. Blank means a HealthKey-minted concept in a quarantined HK-* vocabulary." |
| **Destination Table** | read-only, from Domain | ✅ | "The OMOP clinical table the fact is stored in. Follows from Domain." |

Everything below Destination Concept ID is derived from the resolved concept, so all of it is
read-only except the name.

**Destination Vocabulary ID and the search scope are two different fields.** An earlier draft had
one control doing both, which does not work: the vocabulary *of the concept you have* cannot also
be the vocabulary *you are searching for a replacement in*, and making it read-only would lock a
curator out of re-pointing an `HK-*` mint at a LOINC or SNOMED concept — the primary curation
action. So the destination's vocabulary is read-only and derived, and a separate labelled **Search
vocabulary** select scopes the search, defaulting to the destination's and widenable to all. **"Destination OMOP Concept ID" is renamed "Destination Concept ID"** —
OMOP is always the destination in code mapping, so the word carried no information and made the
longest label on the screen the least informative one.

### 3.2 Domain drives the destination table

| Domain | Destination table |
|---|---|
| Condition | `condition_occurrence` |
| Drug | `drug_exposure` |
| Measurement | `measurement` |
| Observation | `observation` |
| Procedure | `procedure_occurrence` |

The curator never picks the table. It is shown, read-only, so the consequence of the Domain
choice is visible rather than implied.

### 3.3 Tabs and sections

Mirrors the Field Concept Mapping page, which already solves this: tabs pick a slice, and within
a tab two collapsible sections split review work from settled work
(`FieldMappingPage.tsx:96-104`, where `mapping.status === "approved"` is the whole test).

**Tabs** — one per vocabulary that a mapping's **destination concept** belongs to. That is every
`HK-*` vocabulary (today `HK-Drug`, `HK-Labs`, `HK-Language`, `HK-Observation`, `HK-Regimen`,
`HK-Wearable`, plus `HK-Procedure` once a procedure is quarantined —
`regimen_resolution.py:159 _ensure_hk_vocab` creates it on first use) **and the standard
vocabularies a curator re-points into: SNOMED, LOINC, RxNorm, ICD10CM, HemOnc.**

The earlier draft of this plan excluded SNOMED and LOINC on the grounds that they "don't get
mapped". That confused two different things. §1.1 rule 1 says a LOINC/SNOMED code arriving as a
*source* needs no mapping row — true, and unchanged. But the whole point of curation is that an
SME looks at a proposed `HK-*` mapping and re-points its **destination** at a standard concept.
That mapping then has a SNOMED or LOINC destination and needs somewhere to live; without those
tabs the curator's own output would be invisible to them.

A vocabulary with zero mappings still gets a tab so a curator can see the empty bucket. There is
**no "All" tab** — it answers no question a curator has.

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
- Add `domain_id = CharField(max_length=20, blank=True, default='')` — Condition / Drug /
  Measurement / Observation / Procedure. The curator's first choice, and what scopes the source
  code system list and derives `omop_table`.
- Add `source_concept = FK(Concept, null=True)` — the concept for the **source code itself**, when
  that vocabulary is loaded. An ICD-10-CM code has a concept even when it is not the standard one
  the fact should carry, and keeping it distinct from the destination is what stops the two being
  conflated.
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
- New `GET /v1/code-mappings/reference/` returning
  `{domains, source_code_systems_by_domain, destination_vocabularies, omop_tables}`:
  - `domains` — the five, with labels, from `services/source_vocabularies.DOMAIN_CHOICES`
  - `source_code_systems_by_domain` — `{domain_id: [{vocabulary_id, label}, ...]}`, each list led
    by the blank "None — uncoded / free text" option
  - `destination_vocabularies` — standard vocabularies plus every `HK-*`, for the tab strip
  - `omop_tables` — `{domain_id: table}`, so the dialog can show the derived table without
    hardcoding the mapping in the frontend

  Keep `code-mappings/vocabularies/` as a thin alias for one release.
- Re-key `code_mapping_detail` to `code-mappings/<int:mapping_id>/` (`v1_urls.py:128`) and add
  `DELETE` — a curator who mis-keys a source code currently cannot remove the row.
- `_upsert_source_code_mapping` — validate `omop_table`, reject `HK-*` source systems, and stop
  defaulting `source` to `source_vocabulary_id` (that is what wrote `HK-Wearable` into `source`).
- **Delete `propose_all_code_mappings`** and its route. There is no correct version of proposing
  a concept→itself mapping; §4.5 replaces it with proposals generated at import, which is the
  direction that has meaning.

### 4.3 — Frontend: dialog, tabs, sections

`frontend/src/components/CodeMappings/CodeMappingPage.tsx`:

- `MappingForm` carries the full §3.1 field set: `domain_id`, `source_vocabulary_id`,
  `source_code`, `source_code_description`, `source_concept_id`, then
  `destination_concept_id`, `destination_concept_name`, `destination_concept_code`,
  `destination_vocabulary_id`, `destination_concept_class_id`, `standard_concept`, `omop_table`.
- **No unlabelled inputs.** The dialog currently has one holding the source code value. Every
  control gets a visible `<label>` bound by `htmlFor`, and a tooltip (`title` plus an `ⓘ`
  affordance) carrying the §3.1 text — this screen is conceptually dense enough that a field
  whose meaning has to be inferred is a defect.
- Domain drives everything: changing it re-scopes the Source Code System select and re-derives
  the read-only Destination Table.
- Dialog rebuilt to §3.1 with `SOURCE`/`DESTINATION` headings and the provenance line.
  **"Source concept code" → "Source Code"**; the word *concept* never appears on the source side.
- Source Code System and Destination Vocabulary become `<select>`s fed by
  `/v1/code-mappings/reference/`, fetched once on mount with the rows.
- Destination Concept ID is **writable in edit mode too** (it is `readOnly` today at `:487`).
  Typing an id calls `/v1/concepts/<id>/` and back-fills name, vocabulary, class and table;
  an unresolvable id shows an inline error rather than silently saving.
- Destination Vocabulary scopes the concept search (`/v1/concepts/search/?vocabulary_id=…`), so
  a curator who wants a LOINC code is not wading through SNOMED hits.
- Destination Concept Class renders read-only from the resolved concept — extend `ConceptResult`
  with `concept_class_id`, which `/v1/concepts/search/` already returns
  (`views.py:6988 _serialize_concept`).
- The footer button reads **Update & Approve** when it will also flip status to approved, and
  shows the §4.6 progress panel while the re-point request is in flight.
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

Rules 3 and 4 are `_get_or_create_quarantine_concept` (`:172`) as it already stands. Note the
existing shape is *not* three branches: `get_or_create_quarantine_regimen` (`:225`) resolves the
concept — fetch an existing `HK-*` slug or mint one — and then **always** calls
`record_mapping_gap` (`:118`). Two outcomes, with the curation record written unconditionally
alongside the second. That is exactly §1.1 rules 3–4; the only difference is which table holds
the curation record.

The new work is therefore rules 1–2, plus replacing the gap row with a proposed
`SourceCodeConceptMapping`.

**Replace, do not dual-write.** `RegimenMappingGap` has exactly one consumer — the management
command `report_regimen_mapping_gaps.py`, which prints a table to stdout. There is no API
endpoint, no admin registration, no serializer, and nothing in `frontend/src` (verified by
grep). Its own docstring calls itself "the curation queue", so curation today means remembering
to run a CLI command and acting on the printout by hand. Every column it prints has an
equivalent on the proposed mapping once #838 lands:

| gap column | proposed mapping |
|---|---|
| `source_value` | `source_code` |
| `quarantine_concept_id` | `target_concept` — the minted destination |
| `matched_concept_id` | the destination after a curator re-points it |
| `occurrence_count` | `occurrence_count` |
| `last_seen` | `last_seen` |
| `source_system` | `origin_system` |
| `status` | `status` |

The mapping table is a strict superset and has a UI. Dual-writing would maintain two queues for
one job and guarantee they drift, so:

- `resolve_source_code` writes the proposed mapping only;
- a data migration converts existing `RegimenMappingGap` rows into proposed mappings
  (`normalized_name`/`source_value` → `source_code`, `quarantine_concept` → `target_concept`,
  counts and timestamps carried across, `source_system` → `origin_system`);
- `report_regimen_mapping_gaps` is re-pointed at `SourceCodeConceptMapping` filtered to
  `HK-Regimen`/`HK-Drug` destinations, so the CLI keeps working for anyone scripting against it;
- `RegimenMappingGap` itself is dropped in a follow-up once the re-pointed command has run
  clean against staging.

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
`fhir/tests.py:669` asserts it. Neither Measurement path does. Both put the concept *in* the
identity and drop the source value out of it as soon as a concept resolves:

```python
# _insert_discrete_observations (sync.py:484) — ordinary labs
source_key = source_value if not concept_id else None
return (date_value, datetime_value, concept_id, source_key, _norm_num(value))

# _upsert_rollup_observations (sync.py:536) — daily rollups
sv_key = o['sv'] if not o['cid'] else None
```

So a code that resolved to concept A and now resolves to concept B finds nothing at B's key and
**inserts a second row**, stranding the A row beside it. The discrete path is worse: it is
append-only (`_bulk_insert` at `:519`), so it cannot update a concept even in principle.

Fix both by keying on `(measurement_source_value, date)` and leaving the concept out, like the
other four — which is what the bulk-write path
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

### 4.6 — Approving a mapping must re-point the rows already stored

Without this, approval only changes what the *next* import produces. A patient whose bundle is
never re-sent keeps the minted `HK-*` concept forever, and the curator's decision is invisible in
the data — the §2.1 failure again, one level up: the decision is recorded and never acted on.

Trigger: a mapping goes `proposed → approved`, **or** an already-approved mapping's
`target_concept` changes. Both are the same operation — re-point rows from the old destination to
the new one.

`omop_core/management/commands/remap_shadow_concepts.py` already does this job for a different
reason (#415) and has solved the hard parts. Extract its machinery into a service function rather
than writing a second one:

- `_affected_person_ids` (`:227`) — collect the person set from only the columns being rewritten;
  the comment there records that including untouched columns forced needless re-derivation
- `_mark_stale` (`:437`) — `PatientRecord.objects.filter(person_id__in=…).update(derivation_version=0)`,
  marked **before** any write, so an abort mid-loop cannot leave rewritten rows that
  `backfill_patient_records` will never revisit
- the whole block wrapped in `suppress_patient_record_refresh()`
- dry-run by default, `--apply` to write

Decisions specific to this trigger:

- **Match on the old destination, not just the source value.** Rewrite rows where
  `*_source_value = mapping.source_code` **and** `*_concept_id = <previous destination>`. A row
  whose concept someone already corrected by hand must not be clobbered by a later approval.
- **Do not re-derive inline.** The rewrite is a couple of bulk `UPDATE`s and is fast; derivation
  is the expensive half, at 12–32s per bulk-loaded patient (CLAUDE.md → *Deferring the
  PatientRecord Derivation*). There is no task queue in this project — `requirements.txt` has no
  Celery/RQ/django-q — so the approve request rewrites synchronously and marks
  `derivation_version=0`, leaving `backfill_patient_records` to re-derive on its own schedule.
  That is the pattern `remap_shadow_concepts` already uses.
- **Collapse, do not duplicate.** If re-pointing lands a row on an identity another row already
  occupies at the new destination, collapse onto the earliest and delete the rest — the rule
  `_upsert_clinical` applies (`sync.py:840`).
- **Leave the orphaned mint alone by default.** After re-pointing, the `HK-*` concept minted at
  import has no referencing rows. Do not delete it: consumers mirror the concept table per ADR
  0001, so withdrawing a published concept_id is worse than leaving one unreferenced. Offer
  `--delete-orphan-mints`, defaulting off, mirroring `remap_shadow_concepts --keep-mints`.

**The approve button is the trigger.** A curator changes the destination in the dialog and clicks
**Update & Approve**; that one action saves the mapping, flips it to `approved`, and re-points the
stored rows. There is no separate "apply" step to forget.

**It needs progress feedback.** The rewrite touches every clinical row carrying that source code
across every patient, so it can run for a while on a loaded database. The approve response
returns what it did — `{old_concept_id, new_concept_id, rows_updated, persons_marked_stale}` —
and the dialog shows a blocking status panel while the request is in flight:

```
   Updating concept 2000000042 → 3046299
   ████████████░░░░░░░░  rewriting clinical rows…
```

then resolves to `Updated 1,284 rows across 96 patients. 96 patient records queued for
re-derivation.` The panel is not cosmetic: without it a curator sees a frozen dialog and clicks
again, and a second approve of the same mapping is a no-op only because §4.6 matches on the old
destination — which is one more reason that guard matters.

Ship it as both: a service function the approve endpoint calls, and an `apply_approved_mappings`
management command for replay and repair. It shares its core with §4.7 — the same walk over
clinical rows from a different starting point.

Tests: approving a re-pointed mapping updates the stored row in place with **no re-import** and
no row-count change; a hand-corrected row at a third concept is left alone; affected
PatientRecords come back with `derivation_version=0`; the orphaned mint survives by default; a
person with no affected rows is not marked stale.

### 4.7 — Backfill the queue from data already imported

Everything imported before 4.5 left its unresolved codes as `concept_id = 0` rows with the text
in `*_source_value` and nothing in the mapping table. A management command
`propose_mappings_from_unresolved` walks the five clinical tables for `*_concept_id = 0`, groups
by `(source_value, omop_table)`, and runs each through `resolve_source_code` so the existing
backlog appears in the Unmapped tab with real occurrence counts. `--dry-run` first; report what
it would mint before it mints.

This is what replaces the deleted `propose_all_code_mappings`, in the direction that has meaning.
It shares its clinical-row walk with §4.6.

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
5. **Without re-importing**, read the OMOP row back: `*_concept_id` is already the approved
   destination, not the minted `HK-*` concept and not 0 (§4.6).
6. Re-import the same bundle anyway. The row is unchanged and there is still exactly one of it,
   not two (§4.5).

Plus both backend suites (Django runner and pytest) and `npm run lint`, `npm run build`,
`npm test -- --run` green.

## 7. Open questions

- **Retiring `RegimenMappingGap`.** §4.5 migrates its rows into proposed mappings and re-points
  its one CLI consumer, but stops short of dropping the table. Dropping it is a follow-up, after
  the re-pointed command has run clean against staging.
- **Minting a destination from the dialog.** §1.1 puts minting at import, which is where it
  belongs. A curator facing a proposed mapping whose minted concept is wrong can re-point it at
  an Athena concept, but cannot mint a *different* HK-* concept from the dialog. Probably fine —
  flagging it in case SME review turns up a need.

---

## 8. Deviations taken during implementation

Recorded so review can check the reasoning rather than re-derive it.

- **`regimen_resolution.py` was not renamed.** The plan called for renaming it
  `concept_resolution.py`. It has six live callers in `views.py` and renaming it in the same
  change as the behaviour work would have made the diff hard to read. The new entry point lives
  in a new `omop_core/services/code_mapping.py` that imports the existing quarantine helpers, so
  there is still exactly one minting path. The rename is worth doing on its own.
- **`RegimenMappingGap` rows are not yet migrated into proposed mappings.** `resolve_source_code`
  writes only the proposed mapping, as planned, and `propose_mappings_from_unresolved` unions the
  gap table in so nothing is double-proposed. Converting the existing gap rows and re-pointing
  `report_regimen_mapping_gaps` is left as the follow-up §7 already describes.
- **Measurement dedup keeps *both* key forms rather than replacing one with the other.** The plan
  said to drop the concept from the key and key on source value. Doing only that broke a real
  behaviour the concept key was carrying: producers send one LOINC code under varying display
  text, and those are one result. Both paths now match a stored row on **either** key, which
  keeps the display-text collapse and still lets the concept move underneath a row. The test that
  caught it (`fhir/tests.py::test_mapped_observation_dedup_uses_resolved_concept_not_display`)
  was right and the plan was wrong.
- **`repoint_clinical_rows` treats concept 0 as a real previous destination.** `if not
  old_concept_id` skipped it, and 0 — OMOP "No matching concept" — is the single most common
  value a first approval moves rows *off*. Both the service and the view now test `is None`.
- **The page defaults to the tab with review work**, not the first tab. With SNOMED first
  alphabetically and the proposals in `HK-Labs`, the queue looked empty on load.
- **`propose_mappings_from_unresolved` reports already-resolvable codes separately.** Against
  `promop_dev`, 5,737 measurement rows hold `concept_id=0` for LOINC `38483-4`, which resolves
  fine today — they were imported before the vocabulary was loaded. Those need re-pointing, not
  a proposal, and the command says so instead of skipping them silently.
- **A latent bug fixed in passing:** `_ensure_hk_vocab` raised `KeyError` for any `HK-*`
  vocabulary not in its hardcoded dict, so minting into `HK-Labs` or `HK-Condition` crashed. It
  now falls back to a generated name.

---

## 9. Review findings and fixes (PR #845)

A `/code-review high` pass found 13 issues; all were real and all are fixed. The three that
mattered most:

- **The re-point deleted real data.** `_collapse_duplicates` keyed on `(person, date)` alone, so
  a patient with a fasting and a post-prandial glucose on one day lost all but the first when a
  mapping was approved. It now keys on the event identity CLAUDE.md documents for the bulk write
  path — Measurement and Observation carry the raw value columns precisely because several
  distinct results for one analyte on one day are real. Two tests pin both directions: distinct
  same-day results survive, genuine duplicates still collapse.
- **The resolver was not wired into ingest at all.** `resolve_source_code` had no caller outside
  tests and the backfill command, so §1.1 rules 3–4 were not in force: unresolved FHIR codes
  still landed at concept 0 with nothing in the review queue. `_lookup` now takes the OMOP table
  and mints + proposes, with results (including negatives) cached per bundle so query count stays
  flat. The PR description had claimed otherwise; it was wrong.
- **A new approved mapping re-pointed nothing.** `moved` required a previous destination, and a
  freshly created mapping has none — so "New Mapping" on an unresolved code, the most direct
  curation action in the UI, left every stored row at 0 and reported success. It now re-points
  from `NO_MATCHING_CONCEPT_ID`.

Also fixed: a re-import could downgrade a resolved concept back to 0 and delete its neighbours;
two spellings of one rollup in a single bundle produced two rows; PATCH was full-replace, so
approving with `{"status": "approved"}` blanked `omop_table` and silently moved nothing;
`_record_proposal` looked up untruncated and stored truncated, so a >100-char source text
recreated forever and tripped the unique constraint; the re-point matched full-width source codes
against values ingest truncates to 50; collapse orphaned `ProvenanceRecord` rows; a rejected
mapping became permanently unreachable and unrecreatable; `_direct_concept` was
order-nondeterministic; and two functions were left dead.

---

## 10. What the live round trip found

The mocked suites were green — 1759 backend, 401 frontend — while three defects sat in the code.
All three were invisible to a mock by construction, and all are fixed.

- **`GET /v1/concepts/{id}/` did not exist.** The dialog resolves a hand-typed destination id
  against it to back-fill name, code, vocabulary, class and standard flag. The route was never
  added, so the request fell through to the SPA catch-all and returned HTML; the mocked test
  returned a concept and passed. Added `concept_detail` — `concepts/lookup/` translates
  (vocabulary, code) → id and cannot answer the reverse.
- **Approving moved no rows, and said it had.** Ingest writes `_source_text(codeable)` — the
  resource's display text — into `*_source_value`, while a proposal records the *code* when the
  resource carried one. A FHIR Observation coded `SFLC-K` with text
  `SERUM FREE LIGHT CHAIN KAPPA` therefore produced a mapping keyed on the code and a measurement
  keyed on the text, and the re-point matched neither. The approval returned 200 with the row
  still on the minted concept: exactly the silent failure §2.1 is about, reintroduced one level
  down. `_source_value_match` now matches on either.
- **Migration 0191 was edited after it had been applied.** Django records an applied migration and
  will not re-run it, so the two new columns never reached `promop_dev` while the migration state
  reported current. Split into `0192_source_code_mapping_domain`. Editing an applied migration is
  only safe while it is unmerged; a `manage.py migrate` reporting "no migrations to apply" is not
  evidence the schema matches the model — the DB/model sync check in CLAUDE.md is.

The round trip itself lives in `CodeMappingPage.live.test.tsx`, skipped unless
`CODE_MAPPING_LIVE_URL` is set. It drives the real component over real HTTP against a real Django
server on real Postgres: the queue item an import proposed, every dialog control labelled and
tipped, source systems scoped to the domain, and the re-point verified in the database — one row,
moved from the minted `HK-Labs` concept to LOINC 3046299, no duplicate, PatientRecord marked
stale.

---

## 11. Second review pass (12 findings, all fixed)

- **The deprecated alias 500'd on every call.** `code_mapping_vocabularies` returned
  `code_mapping_reference(request)`, but that is `@api_view`-decorated and asserts on
  `django.http.HttpRequest`, so the compatibility path kept "for one release" raised every time and
  no test covered it. Body extracted into a plain helper both views call.
- **The re-point matched on curator free text.** The §10 fix added
  `source_code_description` to the match set, which is right for an import proposal (it holds the
  display text ingest wrote) and unbounded for a curator-typed one — `"Glucose"` against a
  `GLU-3` code would have re-pointed every unrelated producer's Glucose row in the database. Now
  restricted to `origin='import'`. That in turn required dropping the `origin` flip on approve:
  origin is provenance of creation, not who last touched the row, and overwriting it erased the
  fact the description was ingest's.
- **A long free-text source could never take effect.** `_record_proposal` truncated to 100 chars
  while `approved_mapping_for` and the ingest overlay looked up untruncated, so the approved
  mapping never matched — the exact failure the feature exists to fix. All lookups now truncate to
  `SOURCE_CODE_MAX`.
- **Concurrent ingest 500'd whole bundles.** Filter-then-create raced on the unique constraint.
  Savepointed with a re-fetch on conflict; two ETL workers importing one new code is normal, not
  an error.
- **An unloaded LOINC/SNOMED code vanished.** Rule 1 returned before the mint branch, so a code
  whose concept is not loaded on a deploy landed at 0 with nothing in the queue — the likeliest
  way a code goes missing, LOINC being the dominant system for these labs. It now records a
  proposal *without* minting; the fix is a vocabulary load, not a HealthKey concept shadowing a
  real LOINC one. That made `target_concept` nullable, which is the honest model — "seen, no
  destination yet" is a review state — and incidentally fixed an INNER JOIN that hid mappings
  whose concept a vocabulary reload had removed.
- **The two resolution paths disagreed.** The cache overlay wrote a blank-source mapping under
  every vocabulary the value appeared in, including LOINC and SNOMED, overriding the direct hit
  `resolve_source_code` guarantees wins.
- **The new dedup key dropped the concept**, collapsing two same-instant observations whose
  codeable yields no text. The concept is back in the source-value key alongside it.
- **A multi-word concept search could not be typed** — the controlled value was trimmed before
  `setState`, so a space round-tripped away.
- **Destination Concept Name accepted edits and dropped them**; there is no write path for a
  concept name. Read-only, with the tooltip corrected.
- **A coded source could display as "uncoded"** when its system was outside the domain's
  catalogue — the same "source column shows the wrong thing" defect this page exists to remove.
  The stored value is now injected as an option.
- Plus a command summary that reported rows it had not written.

Live round trip re-run after all of it: 4 passed, and the row moved from the minted `HK-Labs`
concept to LOINC 3046299 with no duplicate. One harness note recorded in the test header — the
component's axios uses the `/api` prefix as it does in the browser, so the Vite dev proxy must be
up or every request 404s at :3000 and the page reports "Failed to load code mappings".

---

## 12. Curator-created mappings and queue order (#849)

**Creation is always `proposed`.** A curator could previously create a mapping already approved,
which collapsed writing a mapping and signing it off into one act and skipped the review step the
Unmapped queue exists to provide. It also meant a *create* could rewrite clinical data. Now
approval is the only transition that touches patient rows, which is a much easier property to hold
in your head. The dialog disables Status on a new mapping rather than offering an option the API
would refuse.

**The trap that made this more than a one-line change.** Keying the re-point on "the destination
changed" happens to work when a curator re-points an import's proposal, and silently fails when
they write the mapping themselves: they choose the destination at creation, so by approval time it
has not moved, `moved` is false, and the mapping approves while every unresolved row stays at
concept 0 — reporting success. That is the silent-approval failure §10 and §11 each removed once,
reachable through a third door.

The rule is now what it should always have been: **a human signing off on what a code means is
what moves the rows.** A first approval sweeps `NO_MATCHING_CONCEPT_ID`, claiming the rows its
source code left unresolved; any approval that also moved the destination sweeps the old one.
Both can apply at once and their counts are summed, so the dialog reports everything it touched.

**Who created it lives in `created_by`, not `origin`.** The literal suggestion was to put the
userid in `origin`. `created_by` is already a proper FK holding exactly that (`None` on import
rows), while `origin` is `CharField(max_length=10)` with `choices` — an email does not fit, the
enum would have to go, and it is load-bearing: `_source_value_match` only admits
`source_code_description` into the re-point match set when `origin == 'import'`. So `origin` stays
the machine-vs-human enum, `created_by` carries the person, and the queue sorts on both:

- import proposals first — nobody has decided anything about them yet, which is the work the queue
  is for;
- then humans alphabetically by author, so one curator's drafts stay together instead of
  interleaving with everyone else's by occurrence count;
- then by occurrence count within each group, as before.

The dialog's provenance line reads *"Proposed by import (fhir-sync)"* for machine rows and
*"Created by ada@example.com"* for hand-written ones.
