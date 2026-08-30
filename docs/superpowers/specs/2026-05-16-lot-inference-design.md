# LOT Inference — Design Spec (v2 — ARTEMIS-lite + HealthTree)

**Date:** 2026-05-16
**Updated:** 2026-05-16
**Status:** As-built design
**Issue:** #67 — use OHDSI Artemis gap/switch rules + HealthTree phase-aware myeloma rules to infer lines of therapy

---

## Problem

FHIR imports produce raw `DrugExposure` and `ProcedureOccurrence` rows with no line-of-therapy annotation. Real-world EHR exports list individual drugs (daratumumab, bortezomib, lenalidomide, dexamethasone) without indicating that they form a named regimen (DaraVRD) or which line of therapy that regimen represents. The existing FHIR pipeline only recognises LOT when the input bundle carries a custom `therapy-line` extension — a non-standard annotation that production EHRs do not emit.

The result: `Episode` records and PatientInfo `first/second/later_line_therapy` fields are empty for any patient whose drugs came from a standard EHR export.

---

## Architecture

```
FHIR import
    │
    ▼
DrugExposure rows (raw, per-drug)
ProcedureOccurrence rows (ASCT, CAR-T, BMT)
    │
    ▼
infer_lot_for_person()          ← new service
    │  Phase 1: Collapse overlapping drug periods into drug eras
    │  Phase 2: Merge eras + procedures into combination windows
    │  Phase 3: Segment windows into LOTs using gap + switch + transplant + CAR-T rules
    │  Phase 4: Assign phase labels (induction / consolidation / maintenance / bridging / transplant / CAR T-Cell)
    │  Phase 5: Name each regimen (expanded lookup → alphabetic fallback)
    │  Phase 6: Persist Episode + EpisodeEvent records
    │
    ▼
Episode records (episode_number = LOT, episode_source_value = "VRD (induction)")
EpisodeEvent links (episode ↔ drug_exposure / procedure_occurrence)
    │
    ▼
refresh_patient_record()          ← existing service
    │
    ▼
PatientInfo.first/second/later_line_therapy
```

Inference is triggered:
1. **Automatically** — as a post-FHIR-upload step after `refresh_patient_record`
2. **On demand** — Django management command `manage.py infer_lot` for backfill

---

## Algorithm (ARTEMIS-lite + HealthTree Phase-Aware Rules)

### Phase 1 — Build Drug Eras

For each drug (grouped by `drug_concept_id`, fallback to `drug_source_value`):
- Merge any `DrugExposure` rows that overlap or are within `era_gap` days of each other (default: 30 days)
- Result: a list of `(drug_key, subtype, start_date, end_date)` drug periods per person

**Drug subtype classification** (derived from drug_key):

| Subtype | Description | Examples |
|---|---|---|
| `myeloma` | Active myeloma-targeting agents | bortezomib, lenalidomide, daratumumab, carfilzomib, pomalidomide |
| `cart` | CAR-T cell therapy products | idecabtagene vicleucel, ciltacabtagene autoleucel |
| `steroid` | Corticosteroids | dexamethasone, prednisone, prednisolone, methylprednisolone |
| `mixed` | Other cytotoxics (lymphoma, breast, etc.) | cyclophosphamide, doxorubicin, rituximab, paclitaxel |

A drug era's subtype is assigned by checking `drug_key` against `DRUG_SUBTYPE_MAP` (lowercased match). Unknown drugs default to `mixed`.

**Note on steroid-only windows:** A combination window where ALL active ingredients are steroids does NOT trigger a new LOT or new phase — it is absorbed into the adjacent window.

### Phase 2 — Build Combination Windows (with Procedures)

- Fetch `ProcedureOccurrence` rows for the person; classify by SNOMED concept code:

| SNOMED Code | Event | Subtype |
|---|---|---|
| `425983008` | Peripheral blood stem cell transplant (PBSCT/ASCT) | `transplant` |
| `58776007` | Bone marrow transplant (allogeneic) | `transplant` |
| `1156961008` | CAR-T cell therapy infusion | `cart` |

- Sort all drug eras and procedure events by start_date
- Group drug eras that overlap or start within `combination_window` days (default: 28 days) into a candidate combination; extend end_date as new drugs join
- Procedure events are inserted as single-point events (`start = end = procedure_date`) with their subtype
- Result: a list of `CombinationWindow(drugs: set, procedures: set, subtype: str, start: date, end: date)`

### Phase 3 — Segment into Lines of Therapy

Walk combination windows chronologically applying **all** of the following rules (in priority order):

**1. Transplant rule** (HealthTree — highest priority for myeloma):
- A window containing a `transplant` procedure creates a mandatory LOT boundary
- **Tandem transplant exception:** if a second transplant procedure occurs within 270 days of the first, it is merged into the same LOT (same LOT number, label = `transplant`)

**2. CAR-T rule** (HealthTree):
- A window containing a `cart` procedure creates a mandatory LOT boundary
- If the next drug event starts > 30 days after the CAR-T procedure date, it begins a new LOT

**3. Gap rule** (ARTEMIS-lite):
- Start a new LOT if: `window.start − previous_window.end > gap_threshold` (default: 180 days)

**4. Switch rule** (ARTEMIS-lite):
- Start a new LOT if drugs change significantly:
  `len(added_non_supportive) / len(prev_non_supportive | current_non_supportive) > switch_fraction` (default: 0.50)
- Steroid-class drugs excluded from switch-rule denominator but included in regimen naming

Result: a list of `LineOfTherapy(lot_number, phase_label, windows, start, end)`

### Phase 4 — Assign Phase Labels

After LOT segmentation, each LOT is labelled based on its position relative to transplant/CAR-T events (HealthTree phase logic):

| Condition | Phase Label |
|---|---|
| First LOT, no transplant/CAR-T in patient history yet | `induction` |
| LOT contains a `transplant` procedure | `transplant` |
| LOT contains a `cart` procedure | `CAR T-Cell` |
| LOT starts < 90 days after last transplant end | `consolidation` |
| LOT starts 90–180 days after last transplant end | `maintenance` |
| LOT after prior maintenance without new transplant/CAR-T | `maintenance` |
| LOT that includes bridging drugs given < 30d before CAR-T infusion | `bridging` |
| Otherwise (post-maintenance restart, unclassifiable) | `induction` |

Phase label is stored as part of `episode_source_value`: `"VRD (induction)"`, `"Mel200 (transplant)"`, `"lenalidomide (maintenance)"`.

### Phase 5 — Name Each Regimen

Priority lookup order:

1. **Myeloma acronym table** — check `frozenset` of non-supportive drug keys against `MYELOMA_REGIMEN_LOOKUP` (140+ entries ported from HealthTree's `combinationAcronymList.json` + `myelomaTreatmentAcronyms.js`)
2. **Cross-disease lookup** — check `REGIMEN_LOOKUP` (lymphoma, CLL, breast cancer entries)
3. **Subset match** — if the drug set is a superset of a known regimen (e.g., regimen + supportive agents), match on the known subset
4. **Alphabetic fallback** — join drug names alphabetically with ` + `

### Phase 6 — Persist

For each inferred LOT:
- **Upsert Episode** — match on `(person, episode_number, episode_start_date)`; if found, update `episode_end_date` and `episode_source_value`; else create with `episode_concept_id=32531` (Treatment Regimen)
- **Create EpisodeEvent** — for each `DrugExposure` and `ProcedureOccurrence` in the episode date range, `get_or_create` an `EpisodeEvent` linking it to the episode
- After all episodes persisted: call `refresh_patient_record(person)`

---

## Files

| Action | File | Responsibility |
|---|---|---|
| Create | `omop_core/services/lot_inference_service.py` | Main algorithm (phases 1–6), `infer_lot_for_person()` |
| Create | `omop_core/services/lot_regimens.py` | `MYELOMA_REGIMEN_LOOKUP` (140+ entries), `REGIMEN_LOOKUP` (cross-disease), `DRUG_SUBTYPE_MAP` |
| Create | `omop_core/management/commands/infer_lot.py` | CLI: `manage.py infer_lot [--person-id N] [--all]` |
| Modify | `patient_portal/api/views.py` | Call `infer_lot_for_person()` after FHIR upload refresh |
| Modify | `patient_portal/tests.py` | Add `LotInferenceTest` class |

---

## DRUG_SUBTYPE_MAP (key entries)

```python
DRUG_SUBTYPE_MAP = {
    # Myeloma-targeting agents
    'bortezomib':                    'myeloma',
    'lenalidomide':                  'myeloma',
    'daratumumab':                   'myeloma',
    'carfilzomib':                   'myeloma',
    'pomalidomide':                  'myeloma',
    'elotuzumab':                    'myeloma',
    'isatuximab':                    'myeloma',
    'ixazomib':                      'myeloma',
    'thalidomide':                   'myeloma',
    'selinexor':                     'myeloma',
    'belantamab mafodotin':          'myeloma',
    # CAR-T products
    'idecabtagene vicleucel':        'cart',
    'ciltacabtagene autoleucel':     'cart',
    'lisocabtagene maraleucel':      'cart',
    'axicabtagene ciloleucel':       'cart',
    'tisagenlecleucel':              'cart',
    # Steroids (supportive)
    'dexamethasone':                 'steroid',
    'prednisone':                    'steroid',
    'prednisolone':                  'steroid',
    'methylprednisolone':            'steroid',
    # Default for anything else: 'mixed'
}
```

---

## PROCEDURE_SNOMED_MAP

```python
PROCEDURE_SNOMED_MAP = {
    '425983008': 'transplant',   # PBSCT / ASCT
    '58776007':  'transplant',   # Bone marrow transplant
    '1156961008': 'cart',        # CAR-T cell therapy
}
```

---

## MYELOMA_REGIMEN_LOOKUP (representative entries — full 140+ in lot_regimens.py)

```python
MYELOMA_REGIMEN_LOOKUP = {
    # Core VRD / Dara combinations
    frozenset({'bortezomib', 'lenalidomide', 'dexamethasone'}):                  'VRD',
    frozenset({'daratumumab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):   'DaraVRD',
    frozenset({'daratumumab', 'lenalidomide', 'dexamethasone'}):                 'DaraRD',
    frozenset({'carfilzomib', 'lenalidomide', 'dexamethasone'}):                 'KRD',
    frozenset({'daratumumab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):  'Dara-KRD',
    frozenset({'isatuximab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):   'Isa-KRD',
    frozenset({'melphalan', 'prednisone', 'bortezomib'}):                        'MPV',
    frozenset({'melphalan', 'prednisone', 'thalidomide'}):                       'MPT',
    frozenset({'melphalan', 'prednisone', 'lenalidomide'}):                      'MPR',
    frozenset({'pomalidomide', 'dexamethasone'}):                                'PomDex',
    frozenset({'elotuzumab', 'pomalidomide', 'dexamethasone'}):                  'EPd',
    frozenset({'isatuximab', 'pomalidomide', 'dexamethasone'}):                  'IsaPd',
    frozenset({'daratumumab', 'pomalidomide', 'dexamethasone'}):                 'DaraPd',
    frozenset({'bortezomib', 'cyclophosphamide', 'dexamethasone'}):              'VCD',
    frozenset({'bortezomib', 'dexamethasone'}):                                  'VD',
    frozenset({'thalidomide', 'dexamethasone'}):                                 'ThalDex',
    frozenset({'ixazomib', 'lenalidomide', 'dexamethasone'}):                    'IRD',
    frozenset({'selinexor', 'bortezomib', 'dexamethasone'}):                     'XVd',
    frozenset({'selinexor', 'dexamethasone'}):                                   'Xd',
    frozenset({'carfilzomib', 'dexamethasone'}):                                 'Kd',
    frozenset({'carfilzomib', 'cyclophosphamide', 'dexamethasone'}):             'KCd',
    frozenset({'daratumumab', 'bortezomib', 'dexamethasone'}):                   'DaraVD',
    frozenset({'daratumumab', 'ixazomib', 'dexamethasone'}):                     'DaraId',
    frozenset({'elotuzumab', 'lenalidomide', 'dexamethasone'}):                  'ELd',
    frozenset({'belantamab mafodotin'}):                                          'Belantamab',
    # Conditioning regimens
    frozenset({'melphalan'}):                                                     'Mel200',
    frozenset({'melphalan', 'bortezomib'}):                                      'MelBor',
    frozenset({'busulfan', 'cyclophosphamide'}):                                  'BuCy',
    # ... full 140+ entries in lot_regimens.py
}
```

---

## REGIMEN_LOOKUP (cross-disease — unchanged from v1)

```python
REGIMEN_LOOKUP = {
    # Follicular Lymphoma / DLBCL
    frozenset({'rituximab', 'cyclophosphamide', 'doxorubicin', 'vincristine', 'prednisone'}): 'R-CHOP',
    frozenset({'obinutuzumab', 'cyclophosphamide', 'doxorubicin', 'vincristine', 'prednisone'}): 'G-CHOP',
    frozenset({'rituximab', 'cyclophosphamide', 'vincristine', 'prednisone'}):   'R-CVP',
    frozenset({'rituximab', 'bendamustine'}):                                    'BR',
    frozenset({'obinutuzumab', 'bendamustine'}):                                 'G-B',
    frozenset({'rituximab', 'lenalidomide'}):                                    'R2',
    frozenset({'rituximab'}):                                                    'Rituximab monotherapy',
    # CLL
    frozenset({'fludarabine', 'cyclophosphamide', 'rituximab'}):                 'FCR',
    frozenset({'ibrutinib', 'rituximab'}):                                       'IR',
    frozenset({'ibrutinib'}):                                                    'Ibrutinib',
    frozenset({'venetoclax', 'rituximab'}):                                      'VenR',
    frozenset({'venetoclax', 'obinutuzumab'}):                                   'VenO',
    frozenset({'acalabrutinib', 'obinutuzumab'}):                                'Acala+Obi',
    frozenset({'zanubrutinib'}):                                                 'Zanubrutinib',
    # Breast Cancer
    frozenset({'doxorubicin', 'cyclophosphamide'}):                              'AC',
    frozenset({'paclitaxel', 'doxorubicin', 'cyclophosphamide'}):               'AC-T',
    frozenset({'docetaxel', 'cyclophosphamide'}):                                'TC',
    frozenset({'paclitaxel', 'trastuzumab', 'pertuzumab'}):                     'THP',
    frozenset({'trastuzumab', 'pertuzumab', 'docetaxel'}):                      'TCH+P',
    frozenset({'palbociclib', 'letrozole'}):                                     'Palbociclib+AI',
    frozenset({'ribociclib', 'letrozole'}):                                      'Ribociclib+AI',
    frozenset({'abemaciclib', 'letrozole'}):                                     'Abemaciclib+AI',
    frozenset({'trastuzumab deruxtecan'}):                                       'T-DXd',
    frozenset({'sacituzumab govitecan'}):                                        'SG',
    frozenset({'olaparib'}):                                                     'Olaparib',
    frozenset({'capecitabine'}):                                                 'Capecitabine',
    frozenset({'eribulin'}):                                                     'Eribulin',
    frozenset({'ado-trastuzumab emtansine'}):                                    'T-DM1',
}
```

---

## Tuneable Parameters (with defaults)

| Parameter | Default | Source | Description |
|---|---|---|---|
| `era_gap` | 30 days | ARTEMIS | Max gap between same-drug exposures before treated as separate periods |
| `combination_window` | 28 days | ARTEMIS / HealthTree | Max days between drug start dates to group into one combination |
| `gap_threshold` | 180 days | HealthTree | Treatment-free gap that triggers a new LOT |
| `switch_fraction` | 0.50 | ARTEMIS | Fraction of non-supportive drugs that must change to trigger a new LOT |
| `cart_repeat_threshold` | 30 days | HealthTree | Days after CAR-T infusion before next event triggers new LOT |
| `consolidation_window` | 90 days | HealthTree | Days post-transplant during which subsequent therapy = consolidation |
| `maintenance_window` | 180 days | HealthTree | Days post-transplant after consolidation but before treatment gap = maintenance |
| `tandem_transplant_window` | 270 days | HealthTree | Days within which a second transplant is counted as tandem (same LOT) |

```python
SUPPORTIVE_AGENTS = frozenset({
    'dexamethasone', 'prednisone', 'prednisolone', 'methylprednisolone',
    'filgrastim', 'pegfilgrastim', 'ondansetron', 'granisetron',
    'mesna', 'leucovorin', 'allopurinol', 'rasburicase',
})
```

---

## management command: infer_lot

```
python manage.py infer_lot [--person-id N] [--all] [--dry-run] [--force]
```

- `--person-id N` — run for a single patient
- `--all` — run for all patients who have DrugExposure rows but no Episode records
- `--force` — re-run even when Episodes already exist
- `--dry-run` — print inferred LOTs to stdout without writing to DB
- Output: JSON lines per patient showing inferred LOTs and episode IDs created/updated

---

## FHIR Upload Integration

After the existing `refresh_patient_record(person)` call at the end of FHIR upload processing, add:

```python
from omop_core.services.lot_inference_service import infer_lot_for_person
infer_lot_for_person(person)
```

**Coexistence rule:** If the FHIR bundle already produced `Episode` records (via the therapy-line extension path), `infer_lot_for_person` skips the person (`force=False` default). This preserves the existing annotated-FHIR path and only fills gaps for unannotated imports.

---

## Error Handling

`infer_lot_for_person` wraps all DB writes in a `try/except`. A failure must not block the FHIR upload response. Failures are logged to the `audit` logger with `event=lot_inference_error`.

---

## Tests

New class: `LotInferenceTest` in `patient_portal/tests.py`.

| Test | Assertion |
|---|---|
| `test_single_drug_creates_one_episode` | One drug → one Episode(episode_number=1) |
| `test_combination_window_groups_drugs` | Two drugs starting within 28 days → one Episode |
| `test_gap_rule_creates_new_lot` | Drug gap >180 days → two Episodes |
| `test_switch_rule_creates_new_lot` | >50% drug switch → two Episodes |
| `test_supportive_agent_not_counted_in_switch` | Adding dexamethasone alone does not trigger new LOT |
| `test_regimen_lookup_names_vrd` | Bortezomib + lenalidomide + dexamethasone → 'VRD' |
| `test_regimen_lookup_names_daravrd` | Daratumumab + bortezomib + lenalidomide + dexamethasone → 'DaraVRD' |
| `test_alphabetic_fallback_name` | Unknown combination → alphabetically joined drug names |
| `test_episode_events_linked` | DrugExposure rows in date range linked via EpisodeEvent |
| `test_no_duplicate_episodes` | Running inference twice does not duplicate Episode rows |
| `test_no_duplicate_episode_events` | Running inference twice does not duplicate EpisodeEvent rows |
| `test_patient_info_refreshed` | After inference, PatientInfo.first_line_therapy is populated |
| `test_existing_episodes_skipped` | Person with existing Episodes → inference skips (no change) |
| `test_dry_run_no_db_writes` | `infer_lot_for_person(person, dry_run=True)` → zero DB rows created |
| `test_management_command_single_patient` | `call_command('infer_lot', person_id=N)` → Episodes created |
| `test_transplant_procedure_creates_new_lot` | ASCT (SNOMED 425983008) → two Episodes, second labelled 'transplant' |
| `test_tandem_transplant_same_lot` | Second ASCT within 270d → still one Episode labelled 'transplant' |
| `test_cart_procedure_creates_new_lot` | CAR-T (SNOMED 1156961008) → new Episode labelled 'CAR T-Cell' |
| `test_consolidation_phase_label` | Drug exposure <90d post-transplant → episode_source_value contains 'consolidation' |
| `test_maintenance_phase_label` | Drug exposure 90–180d post-transplant → episode_source_value contains 'maintenance' |
| `test_steroid_only_window_no_new_lot` | Dexamethasone-only window between drug courses → not a separate LOT |
| `test_induction_label_first_lot` | First LOT before any transplant → episode_source_value contains 'induction' |

---

## Out of Scope

- Full OHDSI ARTEMIS R-package integration (TSW sequence alignment against HemOnc concept set)
- Disease-specific combination window customisation (e.g., myeloma 28-day vs lymphoma 21-day cycles) — parameters are global for now
- AI-augmented extraction (Google Gemini path from HealthTree) — future enhancement
- Washout periods and observation window filters (full ARTEMIS)
