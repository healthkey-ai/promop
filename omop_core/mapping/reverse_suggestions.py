"""Find source rows for a selected standard destination; never mutate SCCM.

UMLS and lexical resemblance are retrieval evidence, not equivalence. Optional
ranking checks each source against the destination in the same direction as
ingest. Even a supported match requires an explicit curator decision.
"""
from functools import reduce
from operator import or_
from time import monotonic

from django.contrib.postgres.search import TrigramSimilarity
from django.core.cache import cache
from django.db import OperationalError, transaction, connection
from django.db.models import Case, CharField, Exists, OuterRef, Q, Value, When
from django.db.models.functions import Upper

from omop_core.models import ConceptSynonym, SourceCodeConceptMapping, UmlsSourceCode
from omop_core.services.concept_to_code import concept_payload, eligible_sources, source_payload
from omop_core.services.source_vocabularies import VOCAB_TO_UMLS_ROOT, VOCABULARY_OID_ALIASES
from omop_core.mapping.reverse_catalog import catalog_candidates, catalog_keys


def reverse_retrieval_pool(concept, *, strategies, limit, include_zero_seen=False, timeout_ms=30000):
    return _bounded(timeout_ms, lambda evaluate: _retrieve(
        concept, strategies=strategies, limit=limit, include_zero_seen=include_zero_seen, evaluate=evaluate))


def estimate_reverse_pool(concept, *, strategies, include_zero_seen=False, timeout_ms=8000):
    """How many sources a run could return, by the same queries a run makes.

    A run reads the best ``limit`` rows of each query; this counts them all
    instead. Registered sources are counted exactly in SQL. Unregistered
    vocabulary codes (zero-Seen only) are read as keys, at most CATALOG_CAP per
    query, so ``capped`` makes the total a floor; ``partial`` says that search
    ran out of time and was left out. Raises TimeoutError or a database timeout
    when even the registered count does not fit the budget.
    """
    return _bounded(timeout_ms, lambda evaluate: _estimate(
        concept, strategies=strategies, include_zero_seen=include_zero_seen, evaluate=evaluate))


#: How long an exact estimate stands in for one that runs out of time.
ESTIMATE_CACHE_SECONDS = 24 * 60 * 60
#: Percent of the mapping table's pages a sampled estimate reads.
SAMPLE_PERCENT = 2


def estimate_source_count(concept, *, strategies, include_zero_seen=False,
                          timeout_ms=8000, fallback_ms=4000):
    """:func:`estimate_reverse_pool`, or an approximation when it runs out of time.

    The exact count is borderline on a large queue: the same concept can fit
    the budget once and miss it the next time, on a cold cache or a busy
    database. So a miss falls back to the last exact count for the same
    settings, off only by what was proposed since, and failing that to a
    sample. ``approximate`` is ``'earlier'``, ``'sample'`` or None for exact.
    Raises like :func:`estimate_reverse_pool` when the sample misses as well.
    """
    key = ':'.join(['c2c-estimate', str(concept.pk), ','.join(sorted(strategies)), str(int(include_zero_seen))])
    try:
        estimate = estimate_reverse_pool(concept, strategies=strategies,
                                         include_zero_seen=include_zero_seen, timeout_ms=timeout_ms)
    except (TimeoutError, OperationalError):
        earlier = _cached(cache.get, key)
        if earlier:
            return {**earlier, 'approximate': 'earlier'}
        return _bounded(fallback_ms, lambda evaluate: _sampled(
            concept, strategies=strategies, include_zero_seen=include_zero_seen, evaluate=evaluate))
    _cached(cache.set, key, estimate, ESTIMATE_CACHE_SECONDS)
    return {**estimate, 'approximate': None}


def _cached(operation, *args):
    # An unreachable cache costs the fallback, never the estimate.
    try:
        return operation(*args)
    except Exception:  # noqa: BLE001 - any cache backend error
        return None


def _bounded(timeout_ms, search):
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
            # A callable is a single-value query such as ``queryset.count``.
            return query() if callable(query) else list(query)

        result = search(evaluate)
        # Restore the caller's setting when this is nested in the inline API's
        # transaction. On failure the savepoint rollback restores it instead.
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('statement_timeout', %s, true)", [previous_timeout])
        return result


def _candidate_rows(concept, include_zero_seen):
    rows = eligible_sources(concept)
    return rows if include_zero_seen else rows.filter(occurrence_count__gt=0)


def _umls_rows(concept, rows):
    """Sources sharing a UMLS CUI with the concept, or None without a UMLS root."""
    root = VOCAB_TO_UMLS_ROOT.get(concept.vocabulary_id)
    if not root:
        return None
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
    return with_roots.filter(Exists(sibling))


def _lexical_terms(concept, evaluate):
    # Bound synonym work and use the indexed % operator for each term;
    # similarity() alone would scan the entire source catalog.
    terms = [concept.concept_name, *evaluate(ConceptSynonym.objects.filter(concept=concept).order_by(
        'concept_synonym_name').values_list('concept_synonym_name', flat=True)[:5])]
    if concept.vocabulary_id == 'LOINC':
        # Long LOINC names dilute whole-string similarity: "Albumin
        # (g/dL)" does not match "Albumin [Mass/volume] in Serum or Plasma"
        # at the 0.3 cutoff. Also search the complete component before the
        # bracketed property. This is retrieval only; ranking and approval
        # still use the full destination, including specimen and method.
        component, separator, property_and_specimen = concept.concept_name.partition(' [')
        if separator and ']' in property_and_specimen and len(component.strip()) >= 3:
            terms.append(component.strip())
    return list(dict.fromkeys(term.upper() for term in terms if term))


def _similarity_threshold():
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL pg_trgm.similarity_threshold = 0.3")


def _retrieve(concept, *, strategies, limit, include_zero_seen, evaluate):
    rows = _candidate_rows(concept, include_zero_seen).select_related('target_concept')
    pool = {}
    terms = []

    def collect(matches, strategy):
        for row in evaluate(matches):
            candidate = pool.setdefault(row.pk, {**source_payload(row), 'evidence': []})
            if strategy not in candidate['evidence']:
                candidate['evidence'].append(strategy)
            if strategy == 'lexical':
                candidate['lexical_score'] = max(candidate.get('lexical_score', 0), float(row.score))

    if 'umls' in strategies:
        umls = _umls_rows(concept, rows)
        if umls is not None:
            collect(umls.order_by('-occurrence_count', 'source_code', 'pk')[:limit], 'umls')

    if 'lexical' in strategies:
        terms = _lexical_terms(concept, evaluate)
        with transaction.atomic():
            _similarity_threshold()
            for term in terms:
                matches = rows.annotate(label=Upper('source_code_description')).filter(
                    label__trigram_similar=term,
                ).annotate(score=TrigramSimilarity(Upper('source_code_description'), term)).order_by(
                    '-occurrence_count', '-score', 'source_code', 'pk')[:limit]
                collect(matches, 'lexical')

    # Unregistered vocabulary codes have never been encountered. Search them
    # only when zero-Seen sources are included; keep the preview read-only.
    if include_zero_seen:
        for candidate in catalog_candidates(concept, strategies=strategies, terms=terms, limit=limit, evaluate=evaluate):
            key = candidate['mapping_id'] if candidate['mapping_id'] is not None else (
                candidate['source_vocabulary_id'], candidate['source_code'],
            )
            current = pool.setdefault(key, candidate)
            current['evidence'] = list(dict.fromkeys([*current['evidence'], *candidate['evidence']]))
            current['lexical_score'] = max(current.get('lexical_score', 0), candidate.get('lexical_score', 0))

    return sorted(pool.values(), key=lambda candidate: (
        candidate['occurrence_count'] <= 0, -candidate['occurrence_count'],
        -len(candidate['evidence']), -candidate.get('lexical_score', 0),
        candidate['source_code'], candidate['source_vocabulary_id'], candidate['mapping_id'] or 0,
    ))[:limit]


def _estimate(concept, *, strategies, include_zero_seen, evaluate):
    rows = _candidate_rows(concept, include_zero_seen)
    umls = _umls_rows(concept, rows) if 'umls' in strategies else None
    terms = _lexical_terms(concept, evaluate) if 'lexical' in strategies else []
    counts = {'umls': evaluate(umls.count) if umls is not None else 0, 'lexical': 0}
    both = 0
    lexical = None
    with transaction.atomic():
        _similarity_threshold()
        if terms:
            # One OR of the indexed % per term: a source matching two terms is one source.
            lexical = rows.annotate(label=Upper('source_code_description')).filter(
                reduce(or_, (Q(label__trigram_similar=term) for term in terms)))
            counts['lexical'] = evaluate(lexical.count)
            if counts['umls'] and counts['lexical']:
                both = evaluate(lexical.filter(pk__in=umls.values('pk')).count)
        total = counts['umls'] + counts['lexical'] - both
    capped = partial = False
    if include_zero_seen:
        # Never-seen vocabulary codes are searched across the whole concept
        # table, which a broad name can take longer than the budget to do. The
        # registered count above still stands; the total becomes a floor.
        try:
            with transaction.atomic():
                _similarity_threshold()
                found, registered, capped = catalog_keys(
                    concept, strategies=strategies, terms=terms, evaluate=evaluate)
                # Open mapping rows the vocabulary search reached by a name or
                # synonym their own description does not match.
                ids = set().union(*registered.values())
                counted = set()
                for query in (umls, lexical):
                    if ids and query is not None:
                        counted.update(evaluate(query.filter(pk__in=ids).values_list('pk', flat=True)))
        except (TimeoutError, OperationalError):
            capped = partial = True
        else:
            for evidence, keys in found.items():
                counts[evidence] += len(keys) + len(registered[evidence] - counted)
            total += len(set().union(*found.values())) + len(ids - counted)
    return {'total': total, **counts, 'capped': capped, 'partial': partial}


def _sampled(concept, *, strategies, include_zero_seen, evaluate):
    """The name count over a sample of the mapping table's pages, scaled up.

    Sampled rows cannot use the trigram index, so the cost follows the sample
    size rather than the number of matches -- which is what makes the exact
    count slow for a broad name. A count large enough to time out is large
    enough for a sample to estimate well. UMLS is counted exactly (it is
    cheap), the overlap is ignored, and never-seen vocabulary codes are left
    out (``partial``).
    """
    rows = _candidate_rows(concept, include_zero_seen)
    umls = _umls_rows(concept, rows) if 'umls' in strategies else None
    terms = _lexical_terms(concept, evaluate) if 'lexical' in strategies else []
    counts = {'umls': evaluate(umls.count) if umls is not None else 0, 'lexical': 0}
    if terms:
        _similarity_threshold()
        lexical = rows.annotate(label=Upper('source_code_description')).filter(
            reduce(or_, (Q(label__trigram_similar=term) for term in terms)))
        sql, params = lexical.values('pk').query.sql_with_params()
        table = f'FROM {connection.ops.quote_name(SourceCodeConceptMapping._meta.db_table)}'
        if table not in sql:
            raise TimeoutError('The sampled estimate could not be built.')
        # The first FROM is the outer query's; subqueries come after it.
        sampled = sql.replace(table, f'{table} TABLESAMPLE SYSTEM ({SAMPLE_PERCENT})', 1)

        def count():
            with connection.cursor() as cursor:
                # Django's own compiled query, its values still bound as
                # params; the only text added is the integer SAMPLE_PERCENT.
                cursor.execute(f'SELECT count(*) FROM ({sampled}) sample', params)  # nosec B608
                return cursor.fetchone()[0]

        counts['lexical'] = _two_figures(evaluate(count) * 100 / SAMPLE_PERCENT)
    return {'total': counts['umls'] + counts['lexical'], **counts, 'capped': False,
            'partial': include_zero_seen, 'approximate': 'sample'}


def _two_figures(value):
    """Round to two significant figures: a sample does not support more."""
    value = round(value)
    if value < 100:
        return value
    scale = 10 ** (len(str(value)) - 2)
    return round(value / scale) * scale


def reverse_rank_candidate(concept, candidate, ranking_model):
    if ranking_model == 'none':
        return {**candidate, 'verdict': 'review', 'note': 'Retrieved candidate; clinical equivalence needs review.'}
    from omop_core.mapping.suggestions import chosen_confidence, rank_candidates_dispatch

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
            'confidence': chosen_confidence(chosen, alternatives),
            'alternatives': alternatives or []}
