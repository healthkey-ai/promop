"""Concept search, shared by the in-place API and the packaged one.

Both the curator search in ``patient_portal`` and the embedder's drug/regimen
search need the same semantics: a three-character minimum so the trigram index is
usable, filters that can stand alone against the index, destination narrowing by
default, and rank-by-match ordering instead of primary key. Keeping one
implementation here is what stops the two surfaces from drifting.
"""
import re

from django.db.models import Case, IntegerField, Q, Value, When
from django.db.models.functions import Length
from rest_framework.pagination import PageNumberPagination

# Query params accepted as exact-match filters by every concept endpoint.
CONCEPT_FILTER_PARAMS = ('vocabulary_id', 'domain_id', 'concept_class_id', 'standard_concept')


class ConceptPagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100


class ConceptSearchPagination(ConceptPagination):
    """The curator search's pages: ``limit`` is accepted as ``page_size``.

    The dialogs ask for ``limit`` (8, 25 or 50) and used to get the default 25,
    since only ``page_size`` was read. ``page_size`` wins when both are sent, an
    unusable value falls back to the default, and the cap stays 100. The envelope
    is unchanged. Only the curator search uses this class: the other concept
    endpoints, and the embedder's search, keep ``page_size`` alone.
    """
    limit_query_param = 'limit'

    def get_page_size(self, request):
        # ``page_size`` decides when it is sent, valid or not; ``limit`` only stands in
        # for a request that sends no ``page_size``.
        name = (self.page_size_query_param
                if self.page_size_query_param in request.query_params
                else self.limit_query_param)
        raw = str(request.query_params.get(name) or '').strip()
        if raw.isascii() and raw.isdecimal() and int(raw) > 0:
            return min(int(raw), self.max_page_size)
        return self.page_size


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


def concept_text_search(query):
    """The filter for a typed search box, and the OMOP id it may also name.

    A concept matches by name (``concept_name_search_filter``), by exact
    vocabulary code, or by OMOP concept id. Every branch is indexable, or the
    planner abandons the indexes for all of them (#1466). Numeric codes and ids
    share the box, so both readings are kept, but an arbitrary-length code is
    never cast to a database id. Returns ``(filter, concept_id or None)``; the
    id feeds ``concept_match_ordering``.
    """
    search_filter = concept_name_search_filter(query) | Q(concept_code__iexact=query)
    concept_id = None
    numeric_id = query.lstrip('0') or '0'
    if numeric_id.isascii() and numeric_id.isdecimal() and len(numeric_id) <= 10:
        if int(numeric_id) <= 2_147_483_647:
            concept_id = int(numeric_id)
            search_filter |= Q(concept_id=concept_id)
    return search_filter, concept_id


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


# --- The curator search's ranking (#2923) ---------------------------------------------
#
# ``concept_match_ordering`` alone ranked a LOINC "Prostate specific Ag [Mass/volume] in
# DBS" ahead of the serum test everybody means, because "DBS" is a shorter name; and a
# search for "prostate specific antigen" never reached LOINC at all, whose names say
# "Ag". ``curator_search_queryset`` adds, behind the same match tiers:
#
# * synonym recall: LOINC and SNOMED synonyms ("Prostate specific antigen, Blood"),
#   read from ``suggest_synonym_term``, whose partial trigram indexes are per domain;
# * usage: a concept a curator already chose (an approved field mapping, or a
#   ranked concept) before one an approved code mapping reaches, before the rest;
# * the usual specimen, Serum or Plasma, when the text names no specimen;
# * a shorter code last, which in LOINC and SNOMED is the older, more general concept.
#
# The embedder's drug and regimen search, and the value-concept search, keep using
# ``search_queryset`` and ``concept_match_ordering`` as they were.

# Domains whose synonyms are searched when the caller names none: where abbreviations
# ("Ag", "Ab") and test aliases hide the concept from its own name. A caller that
# filters on one of Suggest's domains gets that domain's synonyms instead.
SYNONYM_DOMAINS = ('Measurement', 'Observation')
# Synonym rows read in all, best first. Broad searches match far more than a page can
# show; the name match covers their tail.
SYNONYM_ROWS = 400
MIN_SYNONYM_TOKEN = 3

# Words that name a specimen. Typing one means the curator chose it, so the usual
# specimen is not preferred over it.
SPECIMEN_WORDS = frozenset({
    'serum', 'plasma', 'blood', 'urine', 'csf', 'fluid', 'semen', 'stool', 'saliva',
    'sputum', 'tissue', 'marrow', 'dbs', 'specimen', 'swab', 'cord', 'amniotic',
    'pleural', 'synovial', 'ascites', 'peritoneal', 'capillary', 'venous', 'arterial',
})
USUAL_SPECIMEN = 'Serum or Plasma'


def _tokens(query):
    return re.findall(r'[^\W_]+', query, flags=re.UNICODE)


def synonym_hits(query, query_params):
    """Concepts whose synonyms match the typed text, by how closely.

    Returns ``{'exact': set, 'prefix': set, 'any': set}`` of concept ids: a synonym
    equal to the text, one that begins with it, and every one found. One indexed
    statement over the domains, capped at ``SYNONYM_ROWS`` rows, best match then
    shortest synonym first. Matching is by every token of the text, as the name
    match is. An empty or unbuilt synonym table gives no hits.
    """
    from omop_core.models import SUGGEST_DESTINATION_DOMAINS, SuggestSynonymTerm

    domain = (query_params.get('domain_id') or '').strip()
    standard = (query_params.get('standard_concept') or '').strip()
    if standard and standard != 'S':
        domains = ()  # the synonym table holds standard concepts only
    elif domain:
        domains = (domain,) if domain in SUGGEST_DESTINATION_DOMAINS else ()
    else:
        domains = SYNONYM_DOMAINS
    upper = query.upper()
    # A trigram index serves a LIKE pattern of three characters or more. Shorter tokens
    # still narrow the match, but only beside one the index can drive: with none, the read
    # would scan a whole domain partition. Text with no such token (``t(4;14)``, ``b 12``)
    # gets name matches only.
    tokens = _tokens(upper)
    if not any(len(t) >= MIN_SYNONYM_TOKEN for t in tokens):
        domains = ()
    hits = {'exact': set(), 'prefix': set(), 'any': set()}
    if not domains:
        return hits
    # ``term`` is stored uppercased and the partial indexes are on the bare column:
    # LIKE against it, never UPPER(term) or ILIKE, or no index is used. Every domain
    # is its own OR branch, an equality the planner can match to that domain's index
    # (``domain_id IN (...)`` it cannot), so the whole read is one statement and a
    # BitmapOr of the partial indexes.
    match = Q()
    for domain_id in domains:
        branch = Q(domain_id=domain_id)
        for token in tokens:
            branch &= Q(term__contains=token)
        match |= branch
    rows = SuggestSynonymTerm.objects.filter(match)
    # The cap is taken after the caller's own filters, so another vocabulary's synonyms
    # cannot crowd out the one asked for. The join runs on the rows the index returns.
    for param in ('vocabulary_id', 'concept_class_id'):
        if query_params.get(param):
            rows = rows.filter(**{f'concept__{param}': query_params[param]})
    rows = rows.annotate(tier=Case(
        When(term=upper, then=Value(1)),
        When(term__startswith=upper, then=Value(2)),
        default=Value(3), output_field=IntegerField(),
    )).order_by('tier', Length('term'), 'concept_id', 'term').values_list('concept_id', 'tier')
    for concept_id, tier in rows[:SYNONYM_ROWS]:
        hits['any'].add(concept_id)
        if tier == 1:
            hits['exact'].add(concept_id)
        elif tier == 2:
            hits['prefix'].add(concept_id)
    return hits


def concept_usage_rank():
    """0 curated, 1 reached by an approved code mapping, 2 neither.

    Curated is a concept an approved field mapping uses or a curator ranked on the
    Concept to Code tab. Both lists are small and ``IN (subquery)`` inside a CASE is
    hashed once per statement, not probed per matched row.
    """
    from omop_core.models import ConceptMappingPriority, FieldConceptMapping, SourceCodeConceptMapping

    curated = (
        Q(concept_id__in=FieldConceptMapping.objects.filter(
            status='approved', concept__isnull=False).values('concept_id'))
        | Q(concept_id__in=ConceptMappingPriority.objects.values('concept_id'))
    )
    mapped = Q(concept_id__in=SourceCodeConceptMapping.objects.filter(
        status='approved', target_concept__isnull=False).values('target_concept_id'))
    return Case(When(curated, then=0), When(mapped, then=1), default=2,
                output_field=IntegerField())


def ranked_concept_ordering(query, concept_id, hits=None):
    """``concept_match_ordering`` plus synonym tiers, usage, specimen and code length."""
    hits = hits or {'exact': (), 'prefix': ()}
    best = Q(concept_code__iexact=query)
    if concept_id is not None:
        best |= Q(concept_id=concept_id)
    exact = Q(concept_name__iexact=query)
    prefix = Q(concept_name__istartswith=query)
    if hits['exact']:
        exact |= Q(concept_id__in=list(hits['exact']))
    if hits['prefix']:
        prefix |= Q(concept_id__in=list(hits['prefix']))
    ordering = [
        Case(When(best, then=0), When(exact, then=1), When(prefix, then=2), default=3,
             output_field=IntegerField()),
        Case(When(standard_concept='S', then=0), default=1, output_field=IntegerField()),
        concept_usage_rank(),
    ]
    if not SPECIMEN_WORDS.intersection(t.lower() for t in _tokens(query)):
        ordering.append(Case(When(concept_name__icontains=USUAL_SPECIMEN, then=0), default=1,
                             output_field=IntegerField()))
    ordering += [Length('concept_name'), Length('concept_code'), 'concept_id']
    return tuple(ordering)


# Bump when the ranking changes: a cached response must not outlive the order it holds.
RANKING_VERSION = 1


def ranking_stamp():
    """What the ranking reads besides the vocabulary, as one short string for the ETag.

    The release ETag alone would let a browser revalidate to a 304 over an order that
    an approval, a ranked concept or a deploy has since changed.
    """
    from django.db.models import Count, Max
    from omop_core.models import ConceptMappingPriority, FieldConceptMapping, SourceCodeConceptMapping

    parts = [str(RANKING_VERSION)]
    for model in (FieldConceptMapping, ConceptMappingPriority):
        agg = model.objects.aggregate(n=Count('pk'), t=Max('updated_at'))
        parts += [str(agg['n']), str(agg['t'])]
    parts.append(str(SourceCodeConceptMapping.objects.aggregate(t=Max('updated_at'))['t']))
    return '|'.join(parts)


def curator_search_queryset(query, query_params):
    """The matched, filtered and ranked queryset behind ``GET /api/v1/concepts/search/``.

    Returns ``(queryset, concept_id)`` like ``search_queryset``, with synonym recall
    and the ranking above. The synonym ids join the match filter as one indexable
    ``concept_id IN (...)`` branch, so the other branches keep their indexes.
    """
    from omop_core.models import Concept

    search_filter, concept_id = concept_text_search(query)
    hits = synonym_hits(query, query_params)
    if hits['any']:
        search_filter |= Q(concept_id__in=list(hits['any']))
    queryset = apply_concept_filters(
        destination_concepts(Concept.objects.filter(search_filter), query_params),
        query_params,
    )
    return queryset.order_by(*ranked_concept_ordering(query, concept_id, hits)), concept_id


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

    search_filter, concept_id = concept_text_search(query)
    queryset = apply_concept_filters(
        destination_concepts((base_queryset or Concept.objects.all()).filter(search_filter),
                             query_params),
        query_params,
    )
    return queryset.order_by(*concept_match_ordering(query, concept_id)), concept_id
