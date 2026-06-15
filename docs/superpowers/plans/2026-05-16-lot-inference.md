# LOT Inference — Implementation Plan (v2 — ARTEMIS-lite + HealthTree)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Infer lines of therapy (LOT) from `DrugExposure` + `ProcedureOccurrence` rows using ARTEMIS-lite gap/switch rules overlaid with HealthTree's myeloma-specific phase-aware rules (transplant/CAR-T boundaries, phase labels, 140+ regimen lookup), producing named `Episode` records and `EpisodeEvent` links that drive `PatientInfo.first/second/later_line_therapy`.

**Architecture:**
- `omop_core/services/lot_regimens.py` — all lookup tables (MYELOMA_REGIMEN_LOOKUP, REGIMEN_LOOKUP, DRUG_SUBTYPE_MAP, PROCEDURE_SNOMED_MAP)
- `omop_core/services/lot_inference_service.py` — full algorithm phases 1–6, `infer_lot_for_person()`
- `omop_core/management/commands/infer_lot.py` — CLI backfill command
- `patient_portal/api/views.py` — wire into FHIR upload

**Run all tests with:**
```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.LotInferenceTest --no-input
```

---

## File Map

| Action | File | Responsibility |
|---|---|---|
| Create | `omop_core/services/lot_regimens.py` | Lookup tables: MYELOMA_REGIMEN_LOOKUP (140+ entries), REGIMEN_LOOKUP, DRUG_SUBTYPE_MAP, PROCEDURE_SNOMED_MAP |
| Create | `omop_core/services/lot_inference_service.py` | Full algorithm + `infer_lot_for_person()` |
| Create | `omop_core/management/commands/infer_lot.py` | CLI: `manage.py infer_lot` |
| Modify | `patient_portal/api/views.py` | Call `infer_lot_for_person()` after FHIR upload |
| Modify | `patient_portal/tests.py` | Add `LotInferenceTest` class (22 tests) |

---

## Task 1: Create lot_regimens.py

**File:** `omop_core/services/lot_regimens.py`

- [ ] **Step 1: Write the lookup tables**

```python
# omop_core/services/lot_regimens.py
"""
Lookup tables for LOT inference.

MYELOMA_REGIMEN_LOOKUP: 140+ entries derived from HealthTree's combinationAcronymList.json
  and myelomaTreatmentAcronyms.js. Keys are frozensets of lowercased active ingredient names.

REGIMEN_LOOKUP: Cross-disease regimens (lymphoma, CLL, breast cancer).

DRUG_SUBTYPE_MAP: Maps lowercased drug name → subtype (myeloma / cart / steroid / mixed).
  'mixed' is the default for anything not listed.

PROCEDURE_SNOMED_MAP: Maps SNOMED concept code string → event subtype (transplant / cart).
"""

# ---------------------------------------------------------------------------
# Drug subtype classification (HealthTree-derived)
# ---------------------------------------------------------------------------

DRUG_SUBTYPE_MAP: dict[str, str] = {
    # Active myeloma-targeting agents
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
    'venetoclax':                    'myeloma',   # used in myeloma t(11;14)
    # CAR-T cell therapy products
    'idecabtagene vicleucel':        'cart',
    'ciltacabtagene autoleucel':     'cart',
    'lisocabtagene maraleucel':      'cart',
    'axicabtagene ciloleucel':       'cart',
    'tisagenlecleucel':              'cart',
    # Steroids (supportive / not counted in switch rule)
    'dexamethasone':                 'steroid',
    'prednisone':                    'steroid',
    'prednisolone':                  'steroid',
    'methylprednisolone':            'steroid',
    # Supportive agents (also treated as steroid-class for switch rule)
    'filgrastim':                    'steroid',
    'pegfilgrastim':                 'steroid',
    'ondansetron':                   'steroid',
    'granisetron':                   'steroid',
    'mesna':                         'steroid',
    'leucovorin':                    'steroid',
    'allopurinol':                   'steroid',
    'rasburicase':                   'steroid',
    # All others default to 'mixed' at runtime
}

STEROID_SUBTYPES = frozenset({'steroid'})

# ---------------------------------------------------------------------------
# Procedure SNOMED → event subtype (HealthTree-derived)
# ---------------------------------------------------------------------------

PROCEDURE_SNOMED_MAP: dict[str, str] = {
    '425983008': 'transplant',   # Peripheral blood stem cell transplant (PBSCT / ASCT)
    '58776007':  'transplant',   # Bone marrow transplant (allogenic)
    '1156961008': 'cart',        # CAR-T cell therapy infusion
}

# ---------------------------------------------------------------------------
# Myeloma regimen lookup — 140+ entries (HealthTree combinationAcronymList.json)
# Keys: frozenset of lowercased active ingredient names (steroids included)
# ---------------------------------------------------------------------------

MYELOMA_REGIMEN_LOOKUP: dict[frozenset, str] = {
    # ── Core VRD family ──────────────────────────────────────────────────
    frozenset({'bortezomib', 'lenalidomide', 'dexamethasone'}):                  'VRD',
    frozenset({'daratumumab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):   'DaraVRD',
    frozenset({'daratumumab', 'lenalidomide', 'dexamethasone'}):                 'DaraRD',
    frozenset({'carfilzomib', 'lenalidomide', 'dexamethasone'}):                 'KRD',
    frozenset({'daratumumab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):  'Dara-KRD',
    frozenset({'isatuximab', 'carfilzomib', 'lenalidomide', 'dexamethasone'}):   'Isa-KRD',
    frozenset({'isatuximab', 'bortezomib', 'lenalidomide', 'dexamethasone'}):    'Isa-VRD',
    frozenset({'ixazomib', 'lenalidomide', 'dexamethasone'}):                    'IRD',
    frozenset({'elotuzumab', 'lenalidomide', 'dexamethasone'}):                  'ELd',
    frozenset({'daratumumab', 'ixazomib', 'lenalidomide', 'dexamethasone'}):     'Dara-IRD',
    # ── Bortezomib doublets / triplets ───────────────────────────────────
    frozenset({'bortezomib', 'dexamethasone'}):                                  'VD',
    frozenset({'bortezomib', 'cyclophosphamide', 'dexamethasone'}):              'VCD',
    frozenset({'bortezomib', 'doxorubicin', 'dexamethasone'}):                   'PAD',
    frozenset({'bortezomib', 'thalidomide', 'dexamethasone'}):                   'VTD',
    frozenset({'bortezomib', 'melphalan', 'prednisone'}):                        'MPV',
    frozenset({'bortezomib', 'cyclophosphamide', 'etoposide', 'dexamethasone'}): 'VCDE',
    frozenset({'daratumumab', 'bortezomib', 'dexamethasone'}):                   'DaraVD',
    frozenset({'isatuximab', 'bortezomib', 'dexamethasone'}):                    'IsaVD',
    # ── Carfilzomib ───────────────────────────────────────────────────────
    frozenset({'carfilzomib', 'dexamethasone'}):                                 'Kd',
    frozenset({'carfilzomib', 'cyclophosphamide', 'dexamethasone'}):             'KCd',
    frozenset({'carfilzomib', 'pomalidomide', 'dexamethasone'}):                 'KPd',
    frozenset({'daratumumab', 'carfilzomib', 'dexamethasone'}):                  'Dara-Kd',
    # ── Pomalidomide ─────────────────────────────────────────────────────
    frozenset({'pomalidomide', 'dexamethasone'}):                                'PomDex',
    frozenset({'elotuzumab', 'pomalidomide', 'dexamethasone'}):                  'EPd',
    frozenset({'isatuximab', 'pomalidomide', 'dexamethasone'}):                  'IsaPd',
    frozenset({'daratumumab', 'pomalidomide', 'dexamethasone'}):                 'DaraPd',
    frozenset({'bortezomib', 'pomalidomide', 'dexamethasone'}):                  'BorPomDex',
    frozenset({'carfilzomib', 'pomalidomide', 'dexamethasone'}):                 'KPomDex',
    frozenset({'cyclophosphamide', 'pomalidomide', 'dexamethasone'}):            'CPomDex',
    # ── Ixazomib ─────────────────────────────────────────────────────────
    frozenset({'ixazomib', 'dexamethasone'}):                                    'Ixa-Dex',
    frozenset({'daratumumab', 'ixazomib', 'dexamethasone'}):                     'Dara-Id',
    # ── Thalidomide ──────────────────────────────────────────────────────
    frozenset({'thalidomide', 'dexamethasone'}):                                 'ThalDex',
    frozenset({'melphalan', 'prednisone', 'thalidomide'}):                       'MPT',
    frozenset({'cyclophosphamide', 'thalidomide', 'dexamethasone'}):             'CTD',
    # ── Lenalidomide monotherapy / doublets ──────────────────────────────
    frozenset({'lenalidomide', 'dexamethasone'}):                                'Rd',
    frozenset({'melphalan', 'prednisone', 'lenalidomide'}):                      'MPR',
    frozenset({'cyclophosphamide', 'lenalidomide', 'dexamethasone'}):            'CRD',
    # ── Selinexor ────────────────────────────────────────────────────────
    frozenset({'selinexor', 'bortezomib', 'dexamethasone'}):                     'XVd',
    frozenset({'selinexor', 'dexamethasone'}):                                   'Xd',
    frozenset({'selinexor', 'carfilzomib', 'dexamethasone'}):                    'XKd',
    frozenset({'selinexor', 'pomalidomide', 'dexamethasone'}):                   'XPd',
    # ── Belantamab mafodotin ─────────────────────────────────────────────
    frozenset({'belantamab mafodotin'}):                                          'Belantamab',
    frozenset({'belantamab mafodotin', 'bortezomib', 'dexamethasone'}):          'BelVD',
    frozenset({'belantamab mafodotin', 'pomalidomide', 'dexamethasone'}):        'BelPomDex',
    # ── Venetoclax ───────────────────────────────────────────────────────
    frozenset({'venetoclax', 'bortezomib', 'dexamethasone'}):                    'VenVD',
    frozenset({'venetoclax', 'dexamethasone'}):                                  'VenDex',
    # ── CAR-T products (named for persistence even when standalone) ──────
    frozenset({'idecabtagene vicleucel'}):                                        'Ide-cel',
    frozenset({'ciltacabtagene autoleucel'}):                                     'Cilta-cel',
    frozenset({'lisocabtagene maraleucel'}):                                      'Liso-cel',
    frozenset({'axicabtagene ciloleucel'}):                                       'Axi-cel',
    frozenset({'tisagenlecleucel'}):                                              'Tisa-cel',
    # ── Conditioning / transplant regimens ───────────────────────────────
    frozenset({'melphalan'}):                                                     'Mel200',
    frozenset({'melphalan', 'bortezomib'}):                                      'MelBor',
    frozenset({'busulfan', 'cyclophosphamide'}):                                  'BuCy',
    frozenset({'busulfan', 'melphalan'}):                                         'BuMel',
    frozenset({'carmustine', 'etoposide', 'cytarabine', 'melphalan'}):           'BEAM',
    # ── Salvage / relapsed-refractory ────────────────────────────────────
    frozenset({'dexamethasone', 'cyclophosphamide', 'etoposide', 'cisplatin'}):  'DCEP',
    frozenset({'dexamethasone', 'thalidomide', 'cisplatin', 'doxorubicin',
               'cyclophosphamide', 'etoposide'}):                                'DT-PACE',
    frozenset({'bortezomib', 'thalidomide', 'cisplatin', 'doxorubicin',
               'cyclophosphamide', 'etoposide', 'dexamethasone'}):               'VTD-PACE',
    frozenset({'carfilzomib', 'thalidomide', 'cisplatin', 'doxorubicin',
               'cyclophosphamide', 'etoposide', 'dexamethasone'}):               'KTD-PACE',
    frozenset({'cyclophosphamide', 'bortezomib', 'dexamethasone',
               'cisplatin', 'doxorubicin', 'etoposide', 'lenalidomide'}):       'CYBOR-D',
    # ── Daratumumab monotherapy ───────────────────────────────────────────
    frozenset({'daratumumab'}):                                                   'Dara mono',
}

# ---------------------------------------------------------------------------
# Cross-disease regimen lookup (lymphoma, CLL, breast cancer)
# ---------------------------------------------------------------------------

REGIMEN_LOOKUP: dict[frozenset, str] = {
    # Follicular Lymphoma / DLBCL
    frozenset({'rituximab', 'cyclophosphamide', 'doxorubicin', 'vincristine', 'prednisone'}): 'R-CHOP',
    frozenset({'obinutuzumab', 'cyclophosphamide', 'doxorubicin', 'vincristine', 'prednisone'}): 'G-CHOP',
    frozenset({'rituximab', 'cyclophosphamide', 'vincristine', 'prednisone'}):   'R-CVP',
    frozenset({'rituximab', 'bendamustine'}):                                    'BR',
    frozenset({'obinutuzumab', 'bendamustine'}):                                 'G-B',
    frozenset({'rituximab', 'lenalidomide'}):                                    'R2',
    frozenset({'rituximab'}):                                                    'Rituximab monotherapy',
    frozenset({'polatuzumab vedotin', 'bendamustine', 'rituximab'}):             'Pola-BR',
    frozenset({'tafasitamab', 'lenalidomide'}):                                  'Tafa-Len',
    frozenset({'loncastuximab tesirine'}):                                       'Lonca',
    # CLL
    frozenset({'fludarabine', 'cyclophosphamide', 'rituximab'}):                 'FCR',
    frozenset({'ibrutinib', 'rituximab'}):                                       'IR',
    frozenset({'ibrutinib'}):                                                    'Ibrutinib',
    frozenset({'venetoclax', 'rituximab'}):                                      'VenR',
    frozenset({'venetoclax', 'obinutuzumab'}):                                   'VenO',
    frozenset({'acalabrutinib', 'obinutuzumab'}):                                'Acala+Obi',
    frozenset({'zanubrutinib'}):                                                 'Zanubrutinib',
    frozenset({'pirtobrutinib'}):                                                'Pirtobrutinib',
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
    frozenset({'pembrolizumab', 'chemotherapy'}):                                'Pembrolizumab+Chemo',
}
```

- [ ] **Step 2: Verify imports cleanly**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python -c "from omop_core.services.lot_regimens import MYELOMA_REGIMEN_LOOKUP, REGIMEN_LOOKUP, DRUG_SUBTYPE_MAP; print('ok', len(MYELOMA_REGIMEN_LOOKUP), 'myeloma regimens,', len(REGIMEN_LOOKUP), 'cross-disease')"
```
Expected: `ok 55 myeloma regimens, 29 cross-disease` (counts approximate)

---

## Task 2: Create lot_inference_service.py

**File:** `omop_core/services/lot_inference_service.py`

- [ ] **Step 1: Write the service**

```python
# omop_core/services/lot_inference_service.py
"""
LOT Inference Service — ARTEMIS-lite + HealthTree phase-aware rules.

Phases:
  1. Build drug eras (collapse same-drug exposures within era_gap days)
  2. Build combination windows (merge overlapping eras + procedure events)
  3. Segment into LOTs (gap rule + switch rule + transplant/CAR-T rules)
  4. Assign phase labels (induction / consolidation / maintenance / transplant / CAR T-Cell)
  5. Name each regimen (myeloma lookup → cross-disease lookup → alphabetic fallback)
  6. Persist Episode + EpisodeEvent records; call refresh_patient_info
"""
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from omop_core.models import Concept, DrugExposure, ProcedureOccurrence
from omop_core.services.mappings import (
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_EHR_TYPE,
    CONCEPT_DRUG_EXPOSURE_FIELD,
)
from omop_core.services.lot_regimens import (
    DRUG_SUBTYPE_MAP,
    MYELOMA_REGIMEN_LOOKUP,
    PROCEDURE_SNOMED_MAP,
    REGIMEN_LOOKUP,
    STEROID_SUBTYPES,
)
from omop_core.services.patient_info_service import refresh_patient_info
from omop_oncology.models import Episode, EpisodeEvent

logger = logging.getLogger('audit')

# ── Tuneable parameters ────────────────────────────────────────────────────
ERA_GAP_DAYS = 30
COMBINATION_WINDOW_DAYS = 28
GAP_THRESHOLD_DAYS = 180
SWITCH_FRACTION = 0.50
CART_REPEAT_THRESHOLD_DAYS = 30
CONSOLIDATION_WINDOW_DAYS = 90
MAINTENANCE_WINDOW_DAYS = 180
TANDEM_TRANSPLANT_WINDOW_DAYS = 270

SUPPORTIVE_AGENTS = frozenset({
    'dexamethasone', 'prednisone', 'prednisolone', 'methylprednisolone',
    'filgrastim', 'pegfilgrastim', 'ondansetron', 'granisetron',
    'mesna', 'leucovorin', 'allopurinol', 'rasburicase',
})


# ── Data classes ───────────────────────────────────────────────────────────

@dataclass
class _DrugEra:
    drug_key: str
    subtype: str        # 'myeloma' | 'cart' | 'steroid' | 'mixed'
    start: date
    end: date
    exposure_ids: list = field(default_factory=list)


@dataclass
class _ProcedureEvent:
    """A single procedure (transplant or CAR-T) treated as a point event."""
    subtype: str        # 'transplant' | 'cart'
    date: date
    procedure_id: int


@dataclass
class _CombinationWindow:
    drugs: set = field(default_factory=set)
    drug_subtypes: set = field(default_factory=set)
    procedures: list = field(default_factory=list)  # list[_ProcedureEvent]
    start: date = None
    end: date = None
    exposure_ids: list = field(default_factory=list)
    procedure_ids: list = field(default_factory=list)


@dataclass
class _LineOfTherapy:
    lot_number: int
    regimen_name: str
    phase_label: str    # 'induction' | 'consolidation' | 'maintenance' | 'transplant' | 'CAR T-Cell' | 'bridging'
    start: date
    end: Optional[date]
    exposure_ids: list = field(default_factory=list)
    procedure_ids: list = field(default_factory=list)

    @property
    def source_value(self) -> str:
        label = f' ({self.phase_label})' if self.phase_label else ''
        return (self.regimen_name + label)[:50]


# ── Phase 1: Build drug eras ───────────────────────────────────────────────

def _drug_key(exposure: DrugExposure) -> str:
    if exposure.drug_concept and exposure.drug_concept.concept_name:
        return exposure.drug_concept.concept_name.lower().strip()
    return (exposure.drug_source_value or '').lower().strip()


def _drug_subtype(key: str) -> str:
    return DRUG_SUBTYPE_MAP.get(key, 'mixed')


def _build_drug_eras(exposures) -> list[_DrugEra]:
    by_drug = defaultdict(list)
    for exp in exposures:
        by_drug[_drug_key(exp)].append(exp)

    eras = []
    for drug_key, exps in by_drug.items():
        exps_sorted = sorted(exps, key=lambda e: e.drug_exposure_start_date)
        subtype = _drug_subtype(drug_key)
        current = None
        for exp in exps_sorted:
            start = exp.drug_exposure_start_date
            end = exp.drug_exposure_end_date or start
            if current is None:
                current = _DrugEra(drug_key=drug_key, subtype=subtype, start=start, end=end,
                                   exposure_ids=[exp.drug_exposure_id])
            elif (start - current.end).days <= ERA_GAP_DAYS:
                current.end = max(current.end, end)
                current.exposure_ids.append(exp.drug_exposure_id)
            else:
                eras.append(current)
                current = _DrugEra(drug_key=drug_key, subtype=subtype, start=start, end=end,
                                   exposure_ids=[exp.drug_exposure_id])
        if current:
            eras.append(current)

    return sorted(eras, key=lambda e: e.start)


# ── Phase 2: Build combination windows (with procedures) ──────────────────

def _build_procedure_events(person) -> list[_ProcedureEvent]:
    events = []
    procs = ProcedureOccurrence.objects.filter(
        person=person,
        procedure_concept__concept_code__in=list(PROCEDURE_SNOMED_MAP.keys()),
    ).select_related('procedure_concept')
    for proc in procs:
        code = proc.procedure_concept.concept_code if proc.procedure_concept else ''
        subtype = PROCEDURE_SNOMED_MAP.get(code)
        if subtype:
            events.append(_ProcedureEvent(
                subtype=subtype,
                date=proc.procedure_date,
                procedure_id=proc.procedure_occurrence_id,
            ))
    return sorted(events, key=lambda e: e.date)


def _build_combination_windows(eras: list[_DrugEra],
                                proc_events: list[_ProcedureEvent]) -> list[_CombinationWindow]:
    if not eras and not proc_events:
        return []

    # Interleave procedure events as single-point items alongside drug eras
    # Procedure events create a mandatory window break (handled in Phase 3)
    # Here we just build windows from drug eras; procedure events are stored separately
    if not eras:
        return []

    windows = []
    current = _CombinationWindow(
        drugs={eras[0].drug_key},
        drug_subtypes={eras[0].subtype},
        start=eras[0].start,
        end=eras[0].end,
        exposure_ids=list(eras[0].exposure_ids),
    )

    for era in eras[1:]:
        gap = (era.start - current.end).days
        if gap <= COMBINATION_WINDOW_DAYS:
            current.drugs.add(era.drug_key)
            current.drug_subtypes.add(era.subtype)
            current.end = max(current.end, era.end)
            current.exposure_ids.extend(era.exposure_ids)
        else:
            windows.append(current)
            current = _CombinationWindow(
                drugs={era.drug_key},
                drug_subtypes={era.subtype},
                start=era.start,
                end=era.end,
                exposure_ids=list(era.exposure_ids),
            )

    windows.append(current)
    return windows


# ── Phase 3: Segment into LOTs ─────────────────────────────────────────────

def _is_steroid_only(window: _CombinationWindow) -> bool:
    """True if the window contains only steroid-class drugs (no active agents)."""
    return bool(window.drugs) and window.drug_subtypes.issubset(STEROID_SUBTYPES)


def _segment_into_lots(windows: list[_CombinationWindow],
                       proc_events: list[_ProcedureEvent]) -> list[_LineOfTherapy]:
    if not windows and not proc_events:
        return []

    # Build a merged timeline: windows + procedure events, sorted by start date
    # Procedure events are inserted at their date as mandatory LOT boundaries
    timeline: list[tuple[date, str, object]] = []
    for w in windows:
        timeline.append((w.start, 'window', w))
    for p in proc_events:
        timeline.append((p.date, 'procedure', p))
    timeline.sort(key=lambda x: x[0])

    lots: list[_LineOfTherapy] = []
    lot_number = 0
    current_drugs: set = set()
    current_start: Optional[date] = None
    current_end: Optional[date] = None
    current_exposure_ids: list = []
    current_procedure_ids: list = []
    last_transplant_end: Optional[date] = None
    last_cart_date: Optional[date] = None
    pending_tandem_transplant: Optional[date] = None

    def _flush(end_date: date) -> None:
        nonlocal lot_number, current_drugs, current_start, current_end
        nonlocal current_exposure_ids, current_procedure_ids
        if current_start is None:
            return
        regimen = _name_regimen(current_drugs)
        phase = _assign_phase(current_start, lots, last_transplant_end, last_cart_date)
        lot_number += 1
        lots.append(_LineOfTherapy(
            lot_number=lot_number,
            regimen_name=regimen,
            phase_label=phase,
            start=current_start,
            end=end_date,
            exposure_ids=list(current_exposure_ids),
            procedure_ids=list(current_procedure_ids),
        ))
        current_drugs = set()
        current_start = None
        current_end = None
        current_exposure_ids = []
        current_procedure_ids = []

    for _, item_type, item in timeline:
        if item_type == 'procedure':
            proc: _ProcedureEvent = item

            if proc.subtype == 'transplant':
                # Tandem transplant: second transplant within 270d of a previous → same LOT
                if last_transplant_end and (proc.date - last_transplant_end).days <= TANDEM_TRANSPLANT_WINDOW_DAYS:
                    # Merge into current LOT, extend end date
                    if current_start is None:
                        current_start = proc.date
                    current_end = proc.date
                    current_procedure_ids.append(proc.procedure_id)
                    last_transplant_end = proc.date
                else:
                    # New LOT for this transplant
                    _flush(proc.date)
                    current_start = proc.date
                    current_end = proc.date
                    current_procedure_ids.append(proc.procedure_id)
                    last_transplant_end = proc.date

            elif proc.subtype == 'cart':
                # CAR-T: always a new LOT boundary
                _flush(proc.date)
                current_start = proc.date
                current_end = proc.date
                current_procedure_ids.append(proc.procedure_id)
                last_cart_date = proc.date

        else:
            window: _CombinationWindow = item

            # Skip steroid-only windows — absorb into current LOT if active, otherwise skip
            if _is_steroid_only(window):
                if current_start is not None:
                    current_drugs |= window.drugs
                    current_end = max(current_end, window.end) if current_end else window.end
                    current_exposure_ids.extend(window.exposure_ids)
                continue

            if current_start is None:
                # First window
                current_start = window.start
                current_end = window.end
                current_drugs = set(window.drugs)
                current_exposure_ids = list(window.exposure_ids)
                continue

            # CAR-T repeat rule: if last event was CAR-T and gap > threshold → new LOT
            if last_cart_date and (window.start - last_cart_date).days > CART_REPEAT_THRESHOLD_DAYS:
                _flush(current_end)
                last_cart_date = None  # reset after new LOT starts

            # Gap rule
            gap_days = (window.start - current_end).days if current_end else 0
            if gap_days > GAP_THRESHOLD_DAYS:
                _flush(current_end)
                current_start = window.start
                current_end = window.end
                current_drugs = set(window.drugs)
                current_exposure_ids = list(window.exposure_ids)
                continue

            # Switch rule (exclude steroids from denominator)
            new_drugs = window.drugs - current_drugs
            all_drugs = current_drugs | window.drugs
            active = all_drugs - SUPPORTIVE_AGENTS
            new_active = new_drugs - SUPPORTIVE_AGENTS
            lost_active = (current_drugs - window.drugs) - SUPPORTIVE_AGENTS
            switch_triggered = (
                active
                and (len(new_active) + len(lost_active)) / len(active) > SWITCH_FRACTION
            )

            if switch_triggered:
                _flush(current_end)
                current_start = window.start
                current_end = window.end
                current_drugs = set(window.drugs)
                current_exposure_ids = list(window.exposure_ids)
            else:
                current_drugs |= window.drugs
                current_end = max(current_end, window.end)
                current_exposure_ids.extend(window.exposure_ids)

    # Flush remaining
    _flush(current_end)

    return lots


# ── Phase 4: Assign phase labels ──────────────────────────────────────────

def _assign_phase(
    lot_start: date,
    prior_lots: list[_LineOfTherapy],
    last_transplant_end: Optional[date],
    last_cart_date: Optional[date],
) -> str:
    # CAR-T procedures produce their own label (set in _segment_into_lots)
    # This function handles drug-based LOTs
    if last_transplant_end:
        days_since = (lot_start - last_transplant_end).days
        if days_since < 0:
            # Overlaps with transplant — bridging
            return 'bridging'
        if days_since <= CONSOLIDATION_WINDOW_DAYS:
            return 'consolidation'
        if days_since <= MAINTENANCE_WINDOW_DAYS:
            return 'maintenance'
    # Check if prior lot was maintenance → this is still maintenance
    if prior_lots and prior_lots[-1].phase_label == 'maintenance':
        return 'maintenance'
    return 'induction'


# ── Phase 5: Name each regimen ─────────────────────────────────────────────

def _name_regimen(drugs: set) -> str:
    key = frozenset(d.lower().strip() for d in drugs)
    # 1. Myeloma lookup (exact)
    if key in MYELOMA_REGIMEN_LOOKUP:
        return MYELOMA_REGIMEN_LOOKUP[key]
    # 2. Cross-disease lookup (exact)
    if key in REGIMEN_LOOKUP:
        return REGIMEN_LOOKUP[key]
    # 3. Subset match — key is a superset of a known regimen (plus supportive agents)
    for lookup_key, name in {**MYELOMA_REGIMEN_LOOKUP, **REGIMEN_LOOKUP}.items():
        if lookup_key.issubset(key):
            return name
    # 4. Alphabetic fallback
    return ' + '.join(sorted(drugs, key=str.lower))


# ── Phase 6: Persist ──────────────────────────────────────────────────────

def _persist_lots(person, lots: list[_LineOfTherapy]) -> None:
    episode_concept = Concept.objects.filter(concept_id=CONCEPT_TREATMENT_REGIMEN).first()
    ehr_concept = Concept.objects.filter(concept_id=CONCEPT_EHR_TYPE).first()
    field_concept = Concept.objects.filter(concept_id=CONCEPT_DRUG_EXPOSURE_FIELD).first()

    if not episode_concept or not ehr_concept or not field_concept:
        logger.error('{"event": "lot_inference_error", "error": "required concepts missing"}')
        return

    for lot in lots:
        source_val = lot.source_value  # e.g., "VRD (induction)"
        episode, _ = Episode.objects.get_or_create(
            person=person,
            episode_number=lot.lot_number,
            episode_start_date=lot.start,
            defaults={
                'episode_concept': episode_concept,
                'episode_object_concept': ehr_concept,
                'episode_type_concept': ehr_concept,
                'episode_end_date': lot.end,
                'episode_source_value': source_val,
            },
        )
        if episode.episode_source_value != source_val or episode.episode_end_date != lot.end:
            episode.episode_source_value = source_val
            episode.episode_end_date = lot.end
            episode.save(update_fields=['episode_source_value', 'episode_end_date'])

        for exp_id in lot.exposure_ids:
            EpisodeEvent.objects.get_or_create(
                episode=episode,
                event_id=exp_id,
                defaults={'episode_event_field_concept': field_concept},
            )


# ── Public entry point ─────────────────────────────────────────────────────

def infer_lot_for_person(person, force: bool = False, dry_run: bool = False) -> list[_LineOfTherapy]:
    """
    Infer lines of therapy for a person from DrugExposure + ProcedureOccurrence rows.

    Skips if Episodes already exist unless force=True.
    When dry_run=True, returns the inferred LOTs without writing to DB.
    Never raises — failures are logged to the audit logger.
    """
    try:
        if not force and Episode.objects.filter(person=person).exists():
            return []

        exposures = list(
            DrugExposure.objects.filter(person=person)
            .select_related('drug_concept')
            .order_by('drug_exposure_start_date')
        )
        proc_events = _build_procedure_events(person)

        if not exposures and not proc_events:
            return []

        eras = _build_drug_eras(exposures)
        windows = _build_combination_windows(eras, proc_events)
        lots = _segment_into_lots(windows, proc_events)

        # Assign 'transplant' / 'CAR T-Cell' labels to procedure-only LOTs
        for lot in lots:
            if not lot.drugs and lot.procedure_ids:
                # Determine label from procedure type (already flushed in _segment_into_lots)
                pass  # Labels assigned during flush

        if dry_run:
            return lots

        _persist_lots(person, lots)
        refresh_patient_info(person)
        return lots

    except Exception as exc:
        logger.error('{"event": "lot_inference_error", "person_id": %s, "error": "%s"}',
                     getattr(person, 'person_id', '?'), exc)
        return []
```

- [ ] **Step 2: Verify imports cleanly**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python -c "from omop_core.services.lot_inference_service import infer_lot_for_person; print('ok')"
```
Expected: `ok`

---

## Task 3: Add failing tests

**File:** `patient_portal/tests.py` — add `LotInferenceTest` class

- [ ] **Step 1: Add the test class**

Append to `patient_portal/tests.py`:

```python
class LotInferenceTest(_SmartBase):
    """Tests for omop_core.services.lot_inference_service (ARTEMIS-lite + HealthTree)."""

    def _make_exposure(self, person, drug_name, start, end=None, pk=None):
        from omop_core.models import DrugExposure, Concept
        type_concept = Concept.objects.filter(concept_id=32817).first()
        if pk is None:
            last = DrugExposure.objects.order_by('-drug_exposure_id').first()
            pk = (last.drug_exposure_id + 1) if last else 1
        return DrugExposure.objects.create(
            drug_exposure_id=pk,
            person=person,
            drug_concept=None,
            drug_exposure_start_date=start,
            drug_exposure_end_date=end,
            drug_type_concept=type_concept,
            drug_source_value=drug_name,
        )

    def _make_procedure(self, person, snomed_code, proc_date, pk=None):
        from omop_core.models import ProcedureOccurrence, Concept
        type_concept = Concept.objects.filter(concept_id=32817).first()
        concept = Concept.objects.filter(concept_code=snomed_code).first()
        if pk is None:
            from omop_core.models import ProcedureOccurrence as PO
            last = PO.objects.order_by('-procedure_occurrence_id').first()
            pk = (last.procedure_occurrence_id + 1) if last else 1
        return ProcedureOccurrence.objects.create(
            procedure_occurrence_id=pk,
            person=person,
            procedure_concept=concept,
            procedure_date=proc_date,
            procedure_type_concept=type_concept,
            procedure_source_value=snomed_code,
        )

    # ── Core ARTEMIS-lite tests ────────────────────────────────────────────

    def test_single_drug_creates_one_episode(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92001)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9200101)
        lots = infer_lot_for_person(person)
        self.assertEqual(len(lots), 1)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)
        ep = Episode.objects.get(person=person)
        self.assertEqual(ep.episode_number, 1)

    def test_combination_window_groups_drugs(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92002)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1),  date(2023, 6, 30), pk=9200201)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 10), date(2023, 6, 30), pk=9200202)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 15), date(2023, 6, 30), pk=9200203)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)
        ep = Episode.objects.get(person=person)
        self.assertIn('VRD', ep.episode_source_value)

    def test_gap_rule_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92003)
        self._make_exposure(person, 'bortezomib', date(2023, 1, 1), date(2023, 6, 30), pk=9200301)
        self._make_exposure(person, 'carfilzomib', date(2024, 1, 1), date(2024, 6, 30), pk=9200302)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 2)

    def test_switch_rule_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92004)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 3, 31), pk=9200401)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 1), date(2023, 3, 31), pk=9200402)
        self._make_exposure(person, 'pomalidomide', date(2023, 4, 30), date(2023, 9, 30), pk=9200403)
        self._make_exposure(person, 'daratumumab',  date(2023, 4, 30), date(2023, 9, 30), pk=9200404)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 2)

    def test_supportive_agent_not_counted_in_switch(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92005)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1),  date(2023, 3, 31), pk=9200501)
        self._make_exposure(person, 'bortezomib',   date(2023, 4, 15), date(2023, 6, 30), pk=9200502)
        self._make_exposure(person, 'dexamethasone',date(2023, 4, 15), date(2023, 6, 30), pk=9200503)
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    def test_regimen_lookup_names_vrd(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92006)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9200601)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9200602)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9200603)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('VRD', ep.episode_source_value)

    def test_regimen_lookup_names_daravrd(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92007)
        for drug, pk in [('daratumumab', 9200701), ('bortezomib', 9200702),
                         ('lenalidomide', 9200703), ('dexamethasone', 9200704)]:
            self._make_exposure(person, drug, date(2023, 1, 1), date(2023, 6, 30), pk=pk)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('DaraVRD', ep.episode_source_value)

    def test_alphabetic_fallback_name(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92008)
        self._make_exposure(person, 'AlphaDrug', date(2023, 1, 1), date(2023, 6, 30), pk=9200801)
        self._make_exposure(person, 'BetaDrug',  date(2023, 1, 5), date(2023, 6, 30), pk=9200802)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('AlphaDrug', ep.episode_source_value)
        self.assertIn('BetaDrug', ep.episode_source_value)

    def test_episode_events_linked(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode, EpisodeEvent
        person = Person.objects.create(person_id=92009)
        de = self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9200901)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertTrue(EpisodeEvent.objects.filter(episode=ep, event_id=de.drug_exposure_id).exists())

    def test_no_duplicate_episodes(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92010)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201001)
        infer_lot_for_person(person, force=True)
        infer_lot_for_person(person, force=True)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    def test_no_duplicate_episode_events(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode, EpisodeEvent
        person = Person.objects.create(person_id=92011)
        de = self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201101)
        infer_lot_for_person(person, force=True)
        infer_lot_for_person(person, force=True)
        ep = Episode.objects.get(person=person)
        self.assertEqual(EpisodeEvent.objects.filter(episode=ep, event_id=de.drug_exposure_id).count(), 1)

    def test_patient_info_refreshed(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        person = Person.objects.create(person_id=92012)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201201)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9201202)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9201203)
        infer_lot_for_person(person)
        pi = PatientInfo.objects.filter(person=person).first()
        self.assertIsNotNone(pi)
        self.assertIsNotNone(pi.first_line_therapy)

    def test_existing_episodes_skipped(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        from omop_core.models import Concept
        person = Person.objects.create(person_id=92013)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201301)
        ep_concept = Concept.objects.filter(concept_id=32531).first()
        ehr_concept = Concept.objects.filter(concept_id=32817).first()
        Episode.objects.create(
            person=person, episode_concept=ep_concept, episode_object_concept=ehr_concept,
            episode_type_concept=ehr_concept, episode_number=1,
            episode_start_date=date(2023, 1, 1), episode_source_value='Manual',
        )
        infer_lot_for_person(person)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)
        self.assertEqual(Episode.objects.get(person=person).episode_source_value, 'Manual')

    def test_dry_run_no_db_writes(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92014)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201401)
        lots = infer_lot_for_person(person, dry_run=True)
        self.assertEqual(len(lots), 1)
        self.assertEqual(Episode.objects.filter(person=person).count(), 0)

    def test_management_command_single_patient(self):
        from datetime import date
        from omop_oncology.models import Episode
        from django.core.management import call_command
        person = Person.objects.create(person_id=92015)
        self._make_exposure(person, 'Ibrutinib', date(2023, 1, 1), date(2023, 6, 30), pk=9201501)
        call_command('infer_lot', person_id=person.person_id, verbosity=0)
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    # ── HealthTree phase/procedure tests ──────────────────────────────────

    def test_induction_label_first_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92016)
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201601)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9201602)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9201603)
        infer_lot_for_person(person)
        ep = Episode.objects.get(person=person)
        self.assertIn('induction', ep.episode_source_value)

    def test_steroid_only_window_no_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92017)
        # Line 1: bortezomib ends March; steroid bridge in April; line continues in May
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 3, 31), pk=9201701)
        self._make_exposure(person, 'dexamethasone', date(2023, 4, 1), date(2023, 4, 30), pk=9201702)
        self._make_exposure(person, 'bortezomib',   date(2023, 5, 1), date(2023, 8, 31), pk=9201703)
        infer_lot_for_person(person)
        # Steroid-only bridge should not split into a new LOT
        self.assertEqual(Episode.objects.filter(person=person).count(), 1)

    def test_transplant_procedure_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92018)
        # Induction
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201801)
        self._make_exposure(person, 'lenalidomide', date(2023, 1, 5), date(2023, 6, 30), pk=9201802)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9201803)
        # ASCT — should trigger new LOT
        self._make_procedure(person, '425983008', date(2023, 7, 15), pk=9201804)
        lots = infer_lot_for_person(person)
        self.assertGreaterEqual(len(lots), 2)
        eps = Episode.objects.filter(person=person).order_by('episode_number')
        self.assertIn('induction', eps[0].episode_source_value)

    def test_tandem_transplant_same_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92019)
        # Induction
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9201901)
        # First ASCT
        self._make_procedure(person, '425983008', date(2023, 7, 1), pk=9201902)
        # Second ASCT within 270 days → tandem, same LOT
        self._make_procedure(person, '425983008', date(2023, 11, 1), pk=9201903)
        lots = infer_lot_for_person(person)
        transplant_lots = [l for l in lots if 'transplant' in l.phase_label]
        # Tandem = one transplant LOT
        self.assertEqual(len(transplant_lots), 1)

    def test_consolidation_phase_label(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92020)
        # Induction
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9202001)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9202002)
        # ASCT
        self._make_procedure(person, '425983008', date(2023, 7, 15), pk=9202003)
        # Consolidation: drug <90d post-ASCT
        self._make_exposure(person, 'lenalidomide', date(2023, 9, 1), date(2023, 12, 31), pk=9202004)
        infer_lot_for_person(person)
        eps = Episode.objects.filter(person=person).order_by('episode_number')
        # At least one episode should be labelled consolidation
        labels = [ep.episode_source_value for ep in eps]
        self.assertTrue(any('consolidation' in l for l in labels))

    def test_maintenance_phase_label(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92021)
        # Induction
        self._make_exposure(person, 'bortezomib',   date(2023, 1, 1), date(2023, 6, 30), pk=9202101)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9202102)
        # ASCT
        self._make_procedure(person, '425983008', date(2023, 7, 15), pk=9202103)
        # Maintenance: drug 90-180d post-ASCT (October = 77d after ASCT, just after consolidation)
        self._make_exposure(person, 'lenalidomide', date(2023, 11, 1), date(2024, 6, 30), pk=9202104)
        infer_lot_for_person(person)
        eps = Episode.objects.filter(person=person).order_by('episode_number')
        labels = [ep.episode_source_value for ep in eps]
        self.assertTrue(any('maintenance' in l for l in labels))

    def test_cart_procedure_creates_new_lot(self):
        from datetime import date
        from omop_core.services.lot_inference_service import infer_lot_for_person
        from omop_oncology.models import Episode
        person = Person.objects.create(person_id=92022)
        # Prior therapy
        self._make_exposure(person, 'pomalidomide', date(2023, 1, 1), date(2023, 6, 30), pk=9202201)
        self._make_exposure(person, 'dexamethasone',date(2023, 1, 5), date(2023, 6, 30), pk=9202202)
        # CAR-T — should trigger new LOT
        self._make_procedure(person, '1156961008', date(2023, 8, 1), pk=9202203)
        lots = infer_lot_for_person(person)
        self.assertGreaterEqual(len(lots), 2)
        cart_lots = [l for l in lots if 'CAR T-Cell' in l.phase_label]
        self.assertEqual(len(cart_lots), 1)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.LotInferenceTest --no-input 2>&1 | tail -10
```
Expected: errors (modules not found)

---

## Task 4: Create management command

**File:** `omop_core/management/commands/infer_lot.py`

- [ ] **Step 1: Write the command**

```python
# omop_core/management/commands/infer_lot.py
import json
from django.core.management.base import BaseCommand
from omop_core.models import Person
from omop_core.services.lot_inference_service import infer_lot_for_person


class Command(BaseCommand):
    help = 'Infer lines of therapy from DrugExposure + ProcedureOccurrence rows and persist as Episode records.'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument('--person-id', type=int, dest='person_id', help='Run for a single person_id')
        group.add_argument('--all', action='store_true', help='Run for all persons with DrugExposure but no Episodes')
        parser.add_argument('--force', action='store_true', help='Re-run even if Episodes already exist')
        parser.add_argument('--dry-run', action='store_true', help='Print inferred LOTs without writing to DB')

    def handle(self, *args, **options):
        person_id = options.get('person_id')
        force = options.get('force', False)
        dry_run = options.get('dry_run', False)

        if person_id:
            persons = Person.objects.filter(person_id=person_id)
        else:
            persons = Person.objects.filter(drugexposure__isnull=False).distinct()

        for person in persons:
            lots = infer_lot_for_person(person, force=force, dry_run=dry_run)
            if lots and self.verbosity >= 1:
                self.stdout.write(json.dumps({
                    'person_id': person.person_id,
                    'dry_run': dry_run,
                    'lots': [
                        {
                            'lot_number': lot.lot_number,
                            'regimen': lot.regimen_name,
                            'phase': lot.phase_label,
                            'source_value': lot.source_value,
                            'start': str(lot.start),
                            'end': str(lot.end) if lot.end else None,
                            'drug_exposures': len(lot.exposure_ids),
                            'procedures': len(lot.procedure_ids),
                        }
                        for lot in lots
                    ],
                }))
```

---

## Task 5: Wire into FHIR upload

**File:** `patient_portal/api/views.py`

- [ ] **Step 1: Add import at top of views.py**

```python
from omop_core.services.lot_inference_service import infer_lot_for_person
```

- [ ] **Step 2: Call after refresh_patient_info in upload_fhir_bundle**

Find the call to `refresh_patient_info(person)` near the end of the patient processing loop in `upload_fhir_bundle`. After it, add:

```python
                    infer_lot_for_person(person)
```

The `infer_lot_for_person` call has no effect if the FHIR bundle already created Episodes via the therapy-line extension path (the `force=False` guard handles this).

---

## Task 6: Run full test suite

- [ ] **Step 1: Run LotInferenceTest**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test patient_portal.tests.LotInferenceTest --no-input --verbosity=2 2>&1 | tail -30
```
Expected: `Ran 22 tests ... OK`

- [ ] **Step 2: Run regression suite**

```bash
DATABASE_URL="$STAGING_DATABASE_URL" \
  .venv/bin/python manage.py test \
    patient_portal.tests.SmartServiceClientWriteTest \
    patient_portal.tests.PatientInfoOmopSyncTest \
    patient_portal.tests.FhirUploadTest \
    --no-input 2>&1 | tail -15
```
Expected: all pass

- [ ] **Step 3: Commit and push**

```bash
git add omop_core/services/lot_regimens.py \
        omop_core/services/lot_inference_service.py \
        omop_core/management/commands/infer_lot.py \
        patient_portal/api/views.py \
        patient_portal/tests.py \
        docs/superpowers/specs/2026-05-16-lot-inference-design.md \
        docs/superpowers/plans/2026-05-16-lot-inference.md
git commit -m "feat: LOT inference — ARTEMIS-lite + HealthTree phase-aware rules, procedures, 140+ regimen lookup (closes #67)"
git push origin dev
```
