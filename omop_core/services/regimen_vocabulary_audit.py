"""Check the HemOnc regimen ids the LOT resolvers can return (#2764).

``lot_regimens`` names regimens from in-memory tables: a drug set, or an
abbreviated regimen name, maps to a hard-coded HemOnc concept_id. Nothing ties
those ids to the vocabulary a deployment has installed. When one is missing,
retired, or the wrong kind of concept, the patient-record derivation can drop
it, keep it as an id with no concept behind it, or name the line after the
wrong concept. When a regimen has no component edges of the kinds the
derivation reads, its lines get no regimen-side component drugs.

That is a vocabulary or table defect, to be fixed at the source, so this only
reports. It checks each id the resolvers can return
(``MYELOMA_REGIMEN_CONCEPT_IDS``, ``REGIMEN_CONCEPT_IDS``,
``_GENERATOR_ALIASES``), not only the ones a patient has reached so far. It
does not check the synthetic generator's ``FL_LOT_WEIGHTS``, and it cannot tell
that a valid regimen id belongs to a different regimen than its table key.
"""
from collections import defaultdict
from dataclasses import dataclass

from django.db.models import Q

from omop_core.models import Concept, ConceptRelationship, resolve_concept_replacement
from omop_core.services import lot_regimens
from omop_core.services.patient_record_service import (
    _COMPONENT_RELATIONSHIP_IDS,
    _is_asserted_regimen,
    _usable_concept_name,
)

_LIVE = Q(invalid_reason__isnull=True) | Q(invalid_reason='')


@dataclass(frozen=True)
class RegimenTargetProblem:
    concept_id: object
    sources: tuple
    reason: str
    replacements: tuple = ()


def _declarations():
    """Yield ``(source, value)`` for every id a resolver table declares."""
    for table_name in ('MYELOMA_REGIMEN_CONCEPT_IDS', 'REGIMEN_CONCEPT_IDS'):
        for drugs, value in getattr(lot_regimens, table_name).items():
            if value is not None:
                yield f"{table_name}[{' + '.join(sorted(drugs))}]", value
    for alias, value in lot_regimens._GENERATOR_ALIASES.items():
        yield f'_GENERATOR_ALIASES[{alias!r}]', value


def declared_regimen_targets() -> dict:
    """Return ``{concept_id: [source, ...]}`` for every integer id the resolvers declare.

    ``MYELOMA_REGIMEN_NAME_TO_CONCEPT_ID`` is built from these same tables, so
    it adds no ids of its own. A value that is not an int is left out here and
    reported by ``regimen_target_problems``.
    """
    targets = defaultdict(list)
    for source, value in _declarations():
        if type(value) is int:
            targets[value].append(source)
    return dict(targets)


def _with_component_edges(concept_ids):
    return set(
        ConceptRelationship.objects
        .filter(_LIVE, concept_1_id__in=concept_ids, relationship_id__in=_COMPONENT_RELATIONSHIP_IDS)
        .values_list('concept_1_id', flat=True)
    )


def _problem_with(concept, has_components):
    if concept is None:
        return 'not in the vocabulary'
    if concept.vocabulary_id != 'HemOnc' or concept.concept_class_id != 'Regimen':
        return f'is {concept.vocabulary_id} {concept.concept_class_id}, not a HemOnc Regimen'
    if concept.invalid_reason:
        return f'is retired (invalid_reason {concept.invalid_reason!r})'
    if concept.standard_concept != 'S':
        return 'is not a standard concept'
    if not (_usable_concept_name(concept) or '').strip():
        return 'has no usable name'
    if not _is_asserted_regimen(concept):
        # The derivation's own test, should it come to differ from the above.
        return 'is not accepted as a HemOnc regimen by the derivation'
    if not has_components:
        return 'has no component edge the derivation reads (' + ', '.join(_COMPONENT_RELATIONSHIP_IDS) + ')'
    return None


def _replacements(concept_id):
    """Live successors that would themselves pass: 'Concept replaced by', then 'Maps to'."""
    candidates = []
    resolved, chain = resolve_concept_replacement(concept_id)
    if len(chain) > 1:
        candidates.append(resolved.concept_id)
    candidates += (
        ConceptRelationship.objects
        .filter(_LIVE, concept_1_id=concept_id, relationship_id='Maps to')
        .exclude(concept_2_id=concept_id)
        .order_by('concept_2_id')
        .values_list('concept_2_id', flat=True)
    )
    candidates = list(dict.fromkeys(candidates))
    concepts = Concept.objects.in_bulk(candidates)
    with_components = _with_component_edges(candidates)
    return tuple(
        (cid, concepts[cid].concept_name) for cid in candidates
        if _problem_with(concepts.get(cid), cid in with_components) is None
    )


def regimen_target_problems() -> list:
    """Every declared id that is not a live, standard, named HemOnc Regimen with components."""
    problems = [
        RegimenTargetProblem(value, (source,), 'is not an integer concept_id')
        for source, value in _declarations() if type(value) is not int
    ]
    targets = declared_regimen_targets()
    concepts = Concept.objects.in_bulk(list(targets))
    with_components = _with_component_edges(list(targets))
    for concept_id in sorted(targets):
        concept = concepts.get(concept_id)
        reason = _problem_with(concept, concept_id in with_components)
        if reason:
            replacements = _replacements(concept_id) if concept is not None else ()
            problems.append(RegimenTargetProblem(concept_id, tuple(targets[concept_id]), reason, replacements))
    return problems
