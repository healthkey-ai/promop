"""Check the HemOnc regimen ids the LOT resolvers can return (#2764).

``lot_regimens`` names regimens from in-memory tables: a drug set, or an
abbreviated regimen name, maps to a hard-coded HemOnc concept_id. Nothing ties
those ids to the vocabulary a deployment has installed. When one is missing,
retired, or points at the wrong kind of concept, the patient-record derivation
either drops it (the line ends up with no id) or reports a concept that is not
the regimen the drugs describe.

That is a vocabulary or table defect, to be fixed at the source, so this only
reports. It checks every declared id, not only the ones a patient has reached
so far: a line can reach any of them on its next refresh.
"""
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Q

from omop_core.models import Concept, ConceptRelationship
from omop_core.services import lot_regimens
from omop_core.services.patient_record_service import _usable_concept_name

_LIVE = Q(invalid_reason__isnull=True) | Q(invalid_reason='')


@dataclass(frozen=True)
class RegimenTargetProblem:
    concept_id: object
    sources: tuple
    reason: str
    maps_to: tuple = ()


def declared_regimen_targets() -> dict:
    """Return ``{concept_id: [source, ...]}`` for every id the resolvers can return.

    ``MYELOMA_REGIMEN_NAME_TO_CONCEPT_ID`` is built from these same tables, so
    it adds no ids of its own.
    """
    targets = defaultdict(list)
    for table_name in ('MYELOMA_REGIMEN_CONCEPT_IDS', 'REGIMEN_CONCEPT_IDS'):
        for drugs, concept_id in getattr(lot_regimens, table_name).items():
            if concept_id is not None:
                targets[concept_id].append(f"{table_name}[{' + '.join(sorted(drugs))}]")
    for alias, concept_id in lot_regimens._GENERATOR_ALIASES.items():
        targets[concept_id].append(f'_GENERATOR_ALIASES[{alias!r}]')
    return dict(targets)


def _problem_with(concept):
    if concept is None:
        return 'not in the vocabulary'
    if concept.vocabulary_id != 'HemOnc' or concept.concept_class_id != 'Regimen':
        return f'is {concept.vocabulary_id} {concept.concept_class_id}, not a HemOnc Regimen'
    if concept.invalid_reason:
        return f'is retired (invalid_reason {concept.invalid_reason!r})'
    if concept.standard_concept != 'S':
        return 'is not a standard concept'
    if _usable_concept_name(concept) is None:
        return 'has no usable name'
    return None


def _live_maps_to(concept_id):
    return tuple(
        ConceptRelationship.objects
        .filter(_LIVE, concept_1_id=concept_id, relationship_id='Maps to')
        .exclude(concept_2_id=concept_id)
        .filter(concept_2__in=Concept.objects.filter(_LIVE))
        .order_by('concept_2_id')
        .values_list('concept_2_id', 'concept_2__concept_name')
    )


def regimen_target_problems() -> list:
    """Every declared id that is not a live, standard, named HemOnc Regimen."""
    targets = declared_regimen_targets()
    int_ids = [cid for cid in targets if type(cid) is int]
    concepts = Concept.objects.in_bulk(int_ids)
    problems = []
    for concept_id in sorted(targets, key=str):
        sources = tuple(targets[concept_id])
        if type(concept_id) is not int:
            problems.append(RegimenTargetProblem(concept_id, sources, 'is not an integer concept_id'))
            continue
        concept = concepts.get(concept_id)
        reason = _problem_with(concept)
        if reason:
            maps_to = _live_maps_to(concept_id) if concept is not None else ()
            problems.append(RegimenTargetProblem(concept_id, sources, reason, maps_to))
    return problems
