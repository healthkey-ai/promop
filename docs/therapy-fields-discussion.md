# Therapy Fields — Design Discussion (2026-06-16)

## What we found: current state

### PatientInfo therapy fields are plain free text

`first_line_therapy`, `second_line_therapy`, `later_therapy` are all `TextField(blank=True, null=True)`.
No FK to Concept, no vocabulary validation, no enforcement.

### Distinct values in promop_dev (queried live)

**first_line_therapy** — 21 distinct values, 334 patients
| Value | Count |
|---|---|
| VRd | 50 |
| KRd | 36 |
| VRd (Bortezomib, Lenalidomide, and Dexamethasone) | 26 |
| DRd | 25 |
| Rd | 18 |
| Tamoxifen | 18 |
| Dara-VRd (Daratumumab, Bortezomib, Lenalidomide, and Dexamethasone) | 18 |
| DVd | 17 |
| CyBorD (Cyclophosphamide, Bortezomib, and Dexamethasone) | 16 |
| Dara-Rd (Daratumumab, Lenalidomide, and Dexamethasone) | 15 |
| AC-T | 13 |
| DKRd | 13 |
| CDK4/6 Inhibitor + Letrozole | 13 |
| Paclitaxel/Trastuzumab/Pertuzumab | 12 |
| KRd (Carfilzomib, Lenalidomide, and Dexamethasone) | 12 |
| TC | 9 |
| VCd | 9 |
| IsaKRd | 8 |
| Td | 7 |
| VRd Lite | 7 |
| Isa-VRd | 6 |

**second_line_therapy** — 22 distinct values, 229 patients

**later_therapy** — 22 distinct values, 104 patients (late-line agents: Teclistamab, Cilta-cel, Ide-cel, etc.)

### Data quality issues identified

1. **Duplicate abbreviation + long-form**: `VRd` and `VRd (Bortezomib, Lenalidomide, and Dexamethasone)` are the same regimen, stored in two formats.
2. **Disease mixing**: Breast cancer regimens (Tamoxifen, AC-T, T-DM1, Capecitabine) mixed with myeloma regimens — not segmented by disease type.
3. **Inconsistent style**: `Belantamab` vs `Belantamab Mafodotin (Blenrep) Monotherapy`, `Teclistamab` vs `Teclistamab (Tecvayli) Monotherapy`.

---

## What HemOnc vocabulary contains

Loaded from Athena: **13,376 HemOnc concepts** including `concept_class_id = 'Regimen'`.

### Exact match check: PatientInfo values vs HemOnc regimen names

Only **6 of 59 distinct therapy values** matched HemOnc exactly (case-insensitive):

| PatientInfo value | HemOnc concept_name | concept_id |
|---|---|---|
| AC-T | AC-T | 35101507 |
| KPd | KPD | 35806324 |
| KRd | KRd | 35806284 |
| Pd | Pd | 35806066 |
| SVd | SVd | 905768 |
| Venetoclax Monotherapy | Venetoclax monotherapy | 35804617 |

**53 values had no HemOnc match**, including the most common ones: VRd, DRd, DVd, DKRd, Rd.

### Why the most common regimens don't match

- **VRd** (76 patients combined) — simply **does not exist** in HemOnc under that abbreviation
- **DRd** (45 patients) — HemOnc name is `Dara-Rd` (concept_id 35806311)
- **DVd** (27 patients) — not found as DVd in HemOnc
- Values with brand names appended: `Selinexor (Xpovio)`, `Cilta-cel (Carvykti) Monotherapy` — don't match because of the suffix

---

## Design discussion: what should we do?

### Option A: Normalize text fields to HemOnc names
Keep as `TextField`, store canonical HemOnc `concept_name` string.

- ✓ No schema change
- ✓ Human-readable in DB and exports
- ✗ Still free text — no DB-level enforcement
- ✗ VRd, DRd, DVd have no HemOnc entry → still no canonical name
- ✗ Can't use ConceptAncestor for ingredient-level queries

### Option B: Replace text with HemOnc concept_id FK
Change fields to `BigIntegerField` / `ForeignKey` to `Concept`.

- ✓ Full OMOP linkage, ConceptAncestor queries
- ✓ Display name always authoritative from Concept table
- ✗ Breaking schema change
- ✗ VRd (most common regimen) has no HemOnc concept → NULL for most patients
- ✗ PatientInfo read model doesn't need normalised FKs — that's what Episode/EpisodeEvent is for

### Option C (chosen): Keep text + add concept_id fields alongside

Add new fields:
- `first_line_therapy_id` — `BigIntegerField`, nullable, HemOnc concept_id
- `second_line_therapy_id` — `BigIntegerField`, nullable, HemOnc concept_id
- `later_therapy_ids` — `JSONField`, list of concept_ids (one per later line)

When a concept_id is resolved, also write the canonical HemOnc `concept_name` into the text field.
Text field = always the display string. Concept_id = present when HemOnc has a matching regimen.

---

## Why PatientInfo still needs these columns (user clarification)

> "The whole purpose of PatientInfo is to do fast matching from a flat table. Its fine to have the detail in Episode/EpisodeEvent but we still need these columns in PatientInfo that summarize first, second and later therapies."

PatientInfo is a **denormalized read model for fast patient matching and querying**.
Episode/EpisodeEvent holds the normalized OMOP truth. Both have a job:

| Layer | Purpose |
|---|---|
| `PatientInfo.first_line_therapy[_id]` | Fast flat-table match: "patients whose 1L was KRd" |
| `Episode` + `EpisodeEvent` + `DrugExposure` | Full OMOP detail: dates, drug-level breakdown, ConceptAncestor roll-up |

---

## Architecture of the current LOT inference pipeline

```
DrugExposure (RxNorm concept_ids)
    ↓ _build_drug_eras()       — collapse same-drug exposures within 30 days
    ↓ _build_combination_windows() — merge drugs starting within 28 days
    ↓ _segment_into_lots()     — apply gap rule (180d) + switch rule (50%)
    ↓ _name_regimen()          — frozenset of drug names → text label via MYELOMA_REGIMEN_LOOKUP
    ↓ _persist_lots()          — write Episode + EpisodeEvent rows
    ↓ refresh_patient_info()   — read Episode records → write PatientInfo therapy text fields
```

`MYELOMA_REGIMEN_LOOKUP` in `omop_oncology/services/lot_regimens.py`:
- Maps `frozenset({'bortezomib','lenalidomide','dexamethasone'})` → `'VRD'`
- 140+ entries
- Returns **text names only** — concept_ids are not stored anywhere today

---

## Planned changes (see full plan)

1. Add 3 new fields to `PatientInfo` + migration (0092)
2. Add `MYELOMA_REGIMEN_CONCEPT_IDS` dict to `lot_regimens.py` — same frozenset keys, HemOnc concept_id values (None where HemOnc has no entry, e.g. VRd)
3. Update `_name_regimen()` to return `(name, concept_id)` tuple
4. Update `_persist_lots()` to store concept_id in `Episode.episode_source_concept_id`
5. Update `_get_treatment_data_from_episodes()` to populate concept_id fields in PatientInfo
6. Serializer: add `*_display` read-only fields that resolve concept_name from concept_id, falling back to text
7. TypeScript: add new field types
8. Backfill management command for existing patients in dev

### Open question: shape of `later_therapy_ids`

Two options under discussion:
- **Flat list** `[35806284, 905768]` — separate field, consistent naming with `first_line_therapy_id`
- **Embed in `later_therapies`** — add `concept_id` key to each existing `{therapy, startDate, endDate}` object

---

## HemOnc concept_ids for known regimens (partial, from live DB query)

| Regimen | PatientInfo text | HemOnc concept_name | concept_id |
|---|---|---|---|
| VRd | VRd | **not in HemOnc** | — |
| DRd | DRd | Dara-Rd | 35806311 |
| KRd | KRd | KRd | 35806284 |
| DVd | DVd | **not confirmed** | — |
| DKRd | DKRd | Dara-KRd | 905602 |
| KPd | KPd | KPD | 35806324 |
| Pd | Pd | Pd | 35806066 |
| SVd | SVd | SVd | 905768 |
| Venetoclax Monotherapy | Venetoclax Monotherapy | Venetoclax monotherapy | 35804617 |
| Belantamab | Belantamab | Belantamab mafodotin monotherapy | 911956 |
| Teclistamab | Teclistamab | Teclistamab monotherapy | 37557075 |
| Cilta-cel | Cilta-cel (Carvykti) Monotherapy | Ciltacabtagene autoleucel monotherapy | 1525038 |
| Selinexor+Dex | Selinexor (Xpovio) | Selinexor and Dexamethasone (Sd) | 35100304 |
| AC-T | AC-T | AC-T | 35101507 |
| Dara-Rd | Dara-Rd (Daratumumab, Lenalidomide...) | Dara-Rd | 35806311 |
| Elo-Rd | EloPd | Elo-Rd | 35806314 |
