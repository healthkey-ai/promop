"""Find source rows for a selected standard destination; never mutate SCCM.

UMLS and lexical resemblance are retrieval evidence, not equivalence. Optional
ranking checks each source against the destination in the same direction as
ingest. Even a supported match requires an explicit curator decision.
"""
from time import monotonic

from django.contrib.postgres.search import TrigramSimilarity
from django.db import transaction, connection
from django.db.models import Case, CharField, Exists, OuterRef, Value, When
from django.db.models.functions import Upper

from omop_core.models import ConceptSynonym, UmlsSourceCode
from omop_core.services.concept_to_code import concept_payload, eligible_sources, source_payload
from omop_core.services.source_vocabularies import VOCAB_TO_UMLS_ROOT, VOCABULARY_OID_ALIASES


def reverse_retrieval_pool(concept, *, strategies, limit, include_zero_seen=False, timeout_ms=30000):
    # Limit the entire search, not each query independently. A synonym set or
    # large UMLS join must not hold an inline gunicorn request indefinitely.
    with transaction.atomic():
        deadline = monotonic() + timeout_ms / 1000
        with connection.cursor() as cursor:
            cursor.execute('SHOW statement_timeout')
            previous_timeout = cursor.fetchone()[0]

        def evaluate(query):
            remaining = int((deadline - monotonic()) * 1000)
            if remaining <= 0:
                raise TimeoutError('Source retrieval exceeded its time budget.')
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, true)", [str(remaining)])
            return list(query)

        result = _retrieve(concept, strategies=strategies, limit=limit,
                           include_zero_seen=include_zero_seen, evaluate=evaluate)
        # Restore the caller's setting when this is nested in the inline API's
        # transaction. On failure the savepoint rollback restores it instead.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous_timeout])
        return result


def _retrieve(concept, *, strategies, limit, include_zero_seen, evaluate):
    rows = eligible_sources(concept).select_related('target_concept')
    if not include_zero_seen:
        rows = rows.filter(occurrence_count__gt=0)
    pool = {}

    def collect(matches, strategy):
        for row in evaluate(matches):
            candidate = pool.setdefault(row.pk, {**source_payload(row), 'evidence': []})
            if strategy not in candidate['evidence']:
                candidate['evidence'].append(strategy)
            if strategy == 'lexical':
                candidate['lexical_score'] = max(candidate.get('lexical_score', 0), float(row.score))

    if 'umls' in strategies:
        root = VOCAB_TO_UMLS_ROOT.get(concept.vocabulary_id)
        if root:
            cuis = UmlsSourceCode.objects.filter(root_source=root, code=concept.concept_code).values('concept_id')
            roots = dict(VOCAB_TO_UMLS_ROOT)
            roots.update({alias: roots[canonical] for alias, canonical in VOCABULARY_OID_ALIASES.items()
                          if canonical in roots})
            with_roots = rows.annotate(umls_root=Case(
                *[When(source_vocabulary_id=vocab, then=Value(sab)) for vocab, sab in roots.items()],
                default=Value(''), output_field=CharField(),
            ))
            sibling = UmlsSourceCode.objects.filter(
                concept_id__in=cuis, root_source=OuterRef('umls_root'), code=OuterRef('source_code'),
            )
            collect(with_roots.filter(Exists(sibling)).order_by('-occurrence_count', 'source_code', 'pk')[:limit], 'umls')

    if 'lexical' in strategies:
        # Bound synonym work and use the indexed % operator for each term;
        # similarity() alone would scan the entire source catalog.
        terms = [concept.concept_name, *evaluate(ConceptSynonym.objects.filter(concept=concept).order_by(
            'concept_synonym_name').values_list('concept_synonym_name', flat=True)[:5])]
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL pg_trgm.similarity_threshold = 0.3")
            for term in dict.fromkeys(term.upper() for term in terms if term):
                matches = rows.annotate(label=Upper('source_code_description')).filter(
                    label__trigram_similar=term,
                ).annotate(score=TrigramSimilarity(Upper('source_code_description'), term)).order_by(
                    '-occurrence_count', '-score', 'source_code', 'pk')[:limit]
                collect(matches, 'lexical')

    return sorted(pool.values(), key=lambda candidate: (
        candidate['occurrence_count'] <= 0, -candidate['occurrence_count'],
        -len(candidate['evidence']), -candidate.get('lexical_score', 0),
        candidate['source_code'], candidate['mapping_id'],
    ))[:limit]


def reverse_rank_candidate(concept, candidate, ranking_model):
    if ranking_model == 'none':
        return {**candidate, 'verdict': 'review', 'note': 'Retrieved candidate; clinical equivalence needs review.'}
    from omop_core.mapping.suggestions import rank_candidates_dispatch

    chosen, note, alternatives, _timings = rank_candidates_dispatch(
        candidate['source_code'], [concept_payload(concept)],
        candidate['source_code_description'], ranking_model=ranking_model,
        require_model_selection=True,
        source_context={
            'code': candidate['source_code'], 'vocabulary_id': candidate['source_vocabulary_id'],
            'original_description': candidate['source_code_description'],
        },
    )
    return {**candidate, 'verdict': 'supported' if chosen else 'review', 'note': note,
            'alternatives': alternatives or []}
