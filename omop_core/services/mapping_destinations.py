"""Imported alternatives and the curator's selected destination."""
from django.db.models import Case, Count, Exists, IntegerField, OuterRef, Q, Subquery, Value, When
from django.db.models.functions import Coalesce

from omop_core.models import MappingDestinationCandidate
from omop_core.services.concept_unit_info import concept_unit_fields


def with_destination_counts(mappings):
    candidates = MappingDestinationCandidate.objects.filter(mapping_id=OuterRef('pk'))
    counts = candidates.order_by().values('mapping_id').annotate(n=Count('pk')).values('n')
    selected = candidates.filter(
        target_vocabulary_id=OuterRef('target_concept__vocabulary_id'),
        target_concept_code=OuterRef('target_concept__concept_code'),
    )
    # Count imported vocabulary/code pairs, even when their OMOP concepts are
    # unavailable. Include a manually selected destination only if it is new.
    return mappings.annotate(destination_count=Coalesce(Subquery(counts), Value(0)) + Case(
        When(Q(target_concept__isnull=False) & ~Q(status='rejected') & ~Exists(selected), then=Value(1)),
        default=Value(0), output_field=IntegerField(),
    ))


def destination_options(mapping):
    options = []
    keys = set()
    for candidate in mapping.destination_candidates.select_related('target_concept').order_by(
        'target_concept__concept_name', 'target_vocabulary_id', 'target_concept_code',
    ):
        concept = candidate.target_concept
        selectable = bool(concept and concept.standard_concept == 'S' and not concept.invalid_reason)
        options.append({
            'concept_id': concept.pk if concept else None,
            'concept_name': concept.concept_name if concept else 'Concept not loaded',
            'concept_code': candidate.target_concept_code,
            'vocabulary_id': candidate.target_vocabulary_id,
            'domain_id': concept.domain_id if concept else '',
            'concept_class_id': concept.concept_class_id if concept else '',
            'standard_concept': concept.standard_concept if concept else None,
            'invalid_reason': concept.invalid_reason if concept else None,
            'selectable': selectable,
            'origins': candidate.origins,
            'selected': bool(concept and concept.pk == mapping.target_concept_id),
            **concept_unit_fields(concept),
        })
        keys.add((candidate.target_vocabulary_id, candidate.target_concept_code))
    concept = mapping.target_concept
    if concept and mapping.status != 'rejected' and (concept.vocabulary_id, concept.concept_code) not in keys:
        options.insert(0, {
            'concept_id': concept.pk, 'concept_name': concept.concept_name,
            'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
            'domain_id': concept.domain_id, 'concept_class_id': concept.concept_class_id,
            'standard_concept': concept.standard_concept, 'invalid_reason': concept.invalid_reason,
            'selectable': concept.standard_concept == 'S' and not concept.invalid_reason,
            'origins': [mapping.origin_system] if mapping.origin_system else [], 'selected': True,
            **concept_unit_fields(concept),
        })
    return options
