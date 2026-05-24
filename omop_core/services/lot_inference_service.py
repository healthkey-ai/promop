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

from omop_core.models import Concept, ConceptAncestor, ConceptRelationship, DrugExposure, ProcedureOccurrence
from omop_core.services.mappings import (
    CONCEPT_TREATMENT_REGIMEN,
    CONCEPT_EHR_TYPE,
    CONCEPT_DRUG_EXPOSURE_FIELD,
)
from omop_core.services.lot_regimens import (
    DRUG_SUBTYPE_MAP,
    HEMONC_CART_CLASSES,
    HEMONC_MYELOMA_CLASSES,
    HEMONC_STEROID_CLASSES,
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
    procedures: list = field(default_factory=list)
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


def _classify_drug(drug_concept_id: int, drug_source_value: str) -> str:
    if drug_concept_id:
        hemonc_ids = list(
            ConceptRelationship.objects.filter(
                concept_1_id=drug_concept_id,
                relationship_id='Maps to',
                concept_2__vocabulary_id='HemOnc',
            ).values_list('concept_2_id', flat=True)
        )
        if hemonc_ids:
            ancestor_names = set(
                ConceptAncestor.objects.filter(
                    descendant_concept_id__in=hemonc_ids,
                ).values_list('ancestor_concept__concept_name', flat=True)
            )
            ancestor_names.update(
                Concept.objects.filter(concept_id__in=hemonc_ids)
                               .values_list('concept_name', flat=True)
            )
            if ancestor_names & HEMONC_CART_CLASSES:
                return 'cart'
            if ancestor_names & HEMONC_MYELOMA_CLASSES:
                return 'myeloma'
            if ancestor_names & HEMONC_STEROID_CLASSES:
                return 'steroid'
            return 'mixed'
    return DRUG_SUBTYPE_MAP.get(drug_source_value.lower().strip(), 'mixed')


def _build_drug_eras(exposures) -> list[_DrugEra]:
    by_drug = defaultdict(list)
    for exp in exposures:
        by_drug[_drug_key(exp)].append(exp)

    eras = []
    for drug_key, exps in by_drug.items():
        exps_sorted = sorted(exps, key=lambda e: e.drug_exposure_start_date)
        rep = exps_sorted[0]
        concept_id = rep.drug_concept_id or 0
        subtype = _classify_drug(concept_id, drug_key)
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
    return bool(window.drugs) and window.drug_subtypes.issubset(STEROID_SUBTYPES)


def _segment_into_lots(windows: list[_CombinationWindow],
                       proc_events: list[_ProcedureEvent]) -> list[_LineOfTherapy]:
    if not windows and not proc_events:
        return []

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
    current_proc_subtypes: set = set()   # 'transplant' / 'cart' for current lot
    last_transplant_end: Optional[date] = None
    last_cart_date: Optional[date] = None

    def _flush(end_date: date) -> None:
        nonlocal lot_number, current_drugs, current_start, current_end
        nonlocal current_exposure_ids, current_procedure_ids, current_proc_subtypes
        if current_start is None:
            return
        regimen = _name_regimen(current_drugs)
        # Procedure-only lots get their phase from the procedure subtype.
        if not current_drugs and 'transplant' in current_proc_subtypes:
            phase = 'transplant'
        elif not current_drugs and 'cart' in current_proc_subtypes:
            phase = 'CAR T-Cell'
        else:
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
        current_proc_subtypes = set()

    for _, item_type, item in timeline:
        if item_type == 'procedure':
            proc: _ProcedureEvent = item

            if proc.subtype == 'transplant':
                if last_transplant_end and (proc.date - last_transplant_end).days <= TANDEM_TRANSPLANT_WINDOW_DAYS:
                    if current_start is None:
                        current_start = proc.date
                    current_end = proc.date
                    current_procedure_ids.append(proc.procedure_id)
                    current_proc_subtypes.add(proc.subtype)
                    last_transplant_end = proc.date
                else:
                    _flush(proc.date)
                    current_start = proc.date
                    current_end = proc.date
                    current_procedure_ids.append(proc.procedure_id)
                    current_proc_subtypes.add(proc.subtype)
                    last_transplant_end = proc.date

            elif proc.subtype == 'cart':
                _flush(proc.date)
                current_start = proc.date
                current_end = proc.date
                current_procedure_ids.append(proc.procedure_id)
                current_proc_subtypes.add(proc.subtype)
                last_cart_date = proc.date

        else:
            window: _CombinationWindow = item

            if _is_steroid_only(window):
                if current_start is not None:
                    current_drugs |= window.drugs
                    current_end = max(current_end, window.end) if current_end else window.end
                    current_exposure_ids.extend(window.exposure_ids)
                continue

            if current_start is None:
                current_start = window.start
                current_end = window.end
                current_drugs = set(window.drugs)
                current_exposure_ids = list(window.exposure_ids)
                continue

            if last_cart_date and (window.start - last_cart_date).days > CART_REPEAT_THRESHOLD_DAYS:
                _flush(current_end)
                last_cart_date = None

            gap_days = (window.start - current_end).days if current_end else 0
            if gap_days > GAP_THRESHOLD_DAYS:
                _flush(current_end)
                current_start = window.start
                current_end = window.end
                current_drugs = set(window.drugs)
                current_exposure_ids = list(window.exposure_ids)
                continue

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

    _flush(current_end)

    return lots


# ── Phase 4: Assign phase labels ──────────────────────────────────────────

def _assign_phase(
    lot_start: date,
    prior_lots: list[_LineOfTherapy],
    last_transplant_end: Optional[date],
    last_cart_date: Optional[date],
) -> str:
    if last_transplant_end:
        days_since = (lot_start - last_transplant_end).days
        if days_since < 0:
            return 'bridging'
        if days_since <= CONSOLIDATION_WINDOW_DAYS:
            return 'consolidation'
        if days_since <= MAINTENANCE_WINDOW_DAYS:
            return 'maintenance'
    if prior_lots and prior_lots[-1].phase_label == 'maintenance':
        return 'maintenance'
    return 'induction'


# ── Phase 5: Name each regimen ─────────────────────────────────────────────

def _name_regimen(drugs: set) -> str:
    key = frozenset(d.lower().strip() for d in drugs)
    if key in MYELOMA_REGIMEN_LOOKUP:
        return MYELOMA_REGIMEN_LOOKUP[key]
    if key in REGIMEN_LOOKUP:
        return REGIMEN_LOOKUP[key]
    for lookup_key, name in {**MYELOMA_REGIMEN_LOOKUP, **REGIMEN_LOOKUP}.items():
        if lookup_key.issubset(key):
            return name
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
        source_val = lot.source_value
        # Look up an existing episode matching this person + lot_number + start date.
        existing = Episode.objects.filter(
            person=person,
            episode_number=lot.lot_number,
            episode_start_date=lot.start,
        ).first()
        if existing:
            episode = existing
            if episode.episode_source_value != source_val or episode.episode_end_date != lot.end:
                episode.episode_source_value = source_val
                episode.episode_end_date = lot.end
                episode.save(update_fields=['episode_source_value', 'episode_end_date'])
        else:
            # Episode.episode_id is BigIntegerField(primary_key=True) — no autoincrement.
            last_ep = Episode.objects.order_by('-episode_id').first()
            new_ep_id = (last_ep.episode_id + 1) if last_ep else 1
            episode = Episode.objects.create(
                episode_id=new_ep_id,
                person=person,
                episode_concept=episode_concept,
                episode_object_concept=ehr_concept,
                episode_type_concept=ehr_concept,
                episode_number=lot.lot_number,
                episode_start_date=lot.start,
                episode_end_date=lot.end,
                episode_source_value=source_val,
            )

        for exp_id in lot.exposure_ids:
            EpisodeEvent.objects.get_or_create(
                episode_id=episode.episode_id,
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

        if dry_run:
            return lots

        _persist_lots(person, lots)
        refresh_patient_info(person)
        return lots

    except Exception as exc:
        logger.error('{"event": "lot_inference_error", "person_id": %s, "error": "%s"}',
                     getattr(person, 'person_id', '?'), exc)
        return []
