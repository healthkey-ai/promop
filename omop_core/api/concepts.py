"""Concept search, shared by the in-place API and the packaged one.

Both the curator search in ``patient_portal`` and the embedder's drug/regimen
search need the same semantics: a three-character minimum so the trigram index is
usable, filters that can stand alone against the index, destination narrowing by
default, and rank-by-match ordering instead of primary key. Keeping one
implementation here is what stops the two surfaces from drifting.
"""
from django.db.models import Case, IntegerField, Q, When
from django.db.models.functions import Length
from rest_framework.pagination import PageNumberPagination

# Query params accepted as exact-match filters by every concept endpoint.
CONCEPT_FILTER_PARAMS = ('vocabulary_id', 'domain_id', 'concept_class_id', 'standard_concept')


class ConceptPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


def _truthy_param(query_params, name):
    return str(query_params.get(name) or '').strip().lower() in {'1', 'true', 'yes'}


def apply_concept_filters(queryset, query_params):
    for param in CONCEPT_FILTER_PARAMS:
        value = query_params.get(param)
        if value:
            queryset = queryset.filter(**{param: value})
    return queryset


def concept_name_search_filter(query):
    """Match a concept name by its literal text or its meaningful tokens.

    A literal ``icontains`` remains the fast path and preserves exact phrase
    semantics. The token fallback makes ordinary punctuation and spacing
    variations useful too: ``Non hod`` finds ``Non-Hodgkin``. Each token is at
    least part of the submitted three-character query, so this never introduces an
    unbounded one-character search.
    """
    import re

    tokens = re.findall(r'[^\W_]+', query, flags=re.UNICODE)
    if len(tokens) < 2:
        return Q(concept_name__icontains=query)

    token_match = Q()
    for token in tokens:
        token_match &= Q(concept_name__icontains=token)
    return Q(concept_name__icontains=query) | token_match


def destination_concepts(queryset, query_params):
    """Narrow a search to what may be a mapping destination (#1465).

    Active only, unless ``include_retired``; standard only, unless
    ``include_non_standard`` or an explicit ``standard_concept`` filter. Concepts
    authored here (``source='HealthKey'``) are non-standard by construction and
    stay searchable.
    """
    if not _truthy_param(query_params, 'include_retired'):
        queryset = queryset.filter(Q(invalid_reason__isnull=True) | Q(invalid_reason=''))
    if not (_truthy_param(query_params, 'include_non_standard')
            or query_params.get('standard_concept')):
        queryset = queryset.filter(Q(standard_concept='S') | Q(source='HealthKey'))
    return queryset


def concept_match_ordering(query, concept_id):
    """Order hits by how well they match (#1466), not by primary key."""
    best = Q(concept_code__iexact=query)
    if concept_id is not None:
        best |= Q(concept_id=concept_id)
    return (
        Case(
            When(best, then=0),
            When(concept_name__iexact=query, then=1),
            When(concept_name__istartswith=query, then=2),
            default=3,
            output_field=IntegerField(),
        ),
        Case(When(standard_concept='S', then=0), default=1, output_field=IntegerField()),
        Length('concept_name'),
        'concept_id',
    )


def serialize_concept_base(concept, versions=None):
    return {
        'concept_id': concept.concept_id,
        'concept_name': concept.concept_name,
        'vocabulary_id': concept.vocabulary_id,
        'vocabulary_version': (versions or {}).get(concept.vocabulary_id),
        'concept_code': concept.concept_code,
        'domain_id': concept.domain_id,
        'concept_class_id': concept.concept_class_id,
        'standard_concept': concept.standard_concept,
        'invalid_reason': concept.invalid_reason,
    }


def search_queryset(query, query_params, base_queryset=None):
    """The matched, filtered, ranked queryset for a concept search.

    Returns ``(queryset, concept_id)``. The caller paginates: the curator surface
    adds ETag revalidation and unit annotations, the embedder does not.
    """
    from omop_core.models import Concept

    search_filter = concept_name_search_filter(query) | Q(concept_code__iexact=query)
    concept_id = None
    numeric_id = query.lstrip('0') or '0'
    if numeric_id.isascii() and numeric_id.isdecimal() and len(numeric_id) <= 10:
        if int(numeric_id) <= 2_147_483_647:
            concept_id = int(numeric_id)
            search_filter |= Q(concept_id=concept_id)
    queryset = apply_concept_filters(
        destination_concepts((base_queryset or Concept.objects.all()).filter(search_filter),
                             query_params),
        query_params,
    )
    return queryset.order_by(*concept_match_ordering(query, concept_id)), concept_id
