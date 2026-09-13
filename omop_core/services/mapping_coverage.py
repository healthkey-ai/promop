"""Reference coverage over existing curation tables; never create catalog copies."""
from collections import Counter
from datetime import date

from django.utils import timezone

from omop_core.models import TherapyClass, TherapyComponent, TherapyRegimen
from omop_core.services.field_values import ValueResolver, choice_queryset, standard_target


def answer_coverage():
    counts = Counter()
    for choice in choice_queryset():
        if choice.retired:
            counts['retired'] += 1
            continue
        counts['total'] += 1
        mapping = getattr(choice, 'value_mapping', None)
        if mapping is None or mapping.status == 'proposed':
            counts['needs_review'] += 1
        elif mapping.status == 'rejected':
            counts['rejected'] += 1
        elif mapping.outcome == 'mapped':
            counts['approved_mapped' if ValueResolver.mapping(choice) else 'invalid_target'] += 1
        else:
            counts[f'reviewed_{mapping.outcome}'] += 1
    return dict(counts)


def therapy_target_disposition(concept, provider):
    """Screen role/validity without relabeling FK presence as clinical review."""
    if concept is None:
        return 'unmapped'
    today = timezone.localdate()
    if (concept.invalid_reason or not date.fromisoformat(str(concept.valid_start_date)) <= today <= date.fromisoformat(str(concept.valid_end_date))
            or not 0 < concept.pk < 2_000_000_000 or concept.source == 'HealthKey'
            or concept.vocabulary_id.startswith('HK-')):
        return 'invalid_target'
    if provider == 'classes':
        if (concept.standard_concept in ('S', 'C') and (
                (concept.vocabulary_id == 'HemOnc' and concept.concept_class_id == 'Component Class')
                or concept.vocabulary_id == 'ATC')):
            return 'classification_only'
        return 'requires_role_review'
    if not standard_target(concept):
        return 'invalid_target'
    if provider == 'regimens':
        if concept.vocabulary_id == 'HemOnc' and concept.concept_class_id == 'Regimen':
            return 'passes_role_screen'
    elif concept.domain_id == 'Drug' and concept.vocabulary_id in ('RxNorm', 'RxNorm Extension', 'HemOnc'):
        return 'passes_role_screen'
    return 'requires_role_review'


def therapy_coverage():
    result = {}
    for provider, model in (('regimens', TherapyRegimen), ('components', TherapyComponent), ('classes', TherapyClass)):
        counts = Counter()
        for reference in model.objects.select_related('concept'):
            counts['total'] += 1
            counts[therapy_target_disposition(reference.concept, provider)] += 1
        result[provider] = dict(counts)
    return result
