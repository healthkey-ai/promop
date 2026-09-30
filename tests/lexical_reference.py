"""Frozen lexical implementation from 8798ec24, before the top-N probe.

Keep this independent of the optimized query helpers: exact differential tests
must notice missing candidates, changed scores, and changed tie ordering.
"""
from django.contrib.postgres.search import TrigramSimilarity
from django.db.models import Max
from django.db.models.functions import Upper
from omop_core.models import Concept, ConceptSynonym, SuggestSynonymTerm
from omop_core.mapping.suggestions import (
    CANDIDATE_LIMIT, MIN_TRIGRAM_SCORE, SYNONYM_BONUS, STRATEGY_LEXICAL,
    _narrowing_filter,
)


def _name_matches(query: str, domain_id: str | None, limit: int,
                  narrowing: dict[str, str]):
    """Concepts whose name matches, narrowed however the caller asked."""
    return (
        Concept.objects
        .filter(standard_concept='S', invalid_reason__isnull=True,
                **({'domain_id': domain_id} if domain_id else {}))
        .annotate(name_upper=Upper('concept_name'))
        .filter(**narrowing)
        .annotate(score=TrigramSimilarity(Upper('concept_name'), query))
        .filter(score__gt=MIN_TRIGRAM_SCORE)
        # Equal scores are common, so every slice and the final sort tiebreak on id.
        .order_by('-score', 'concept_id')[:limit]
    )


def lexical_candidates(source_value, domain_id, limit=CANDIDATE_LIMIT):
    """Concepts whose name or synonyms look like this source value.

    Scoped to the domain when supplied; ICD-10 searches all domains. Standard
    concepts only -- a curator re-pointing at a non-standard one is a decision
    they can still make by hand, but it is never what we should suggest.
    """
    query = (source_value or '').strip().upper()
    if len(query) < 3:
        return []

    # Narrow with `%` first, score second.
    #
    # TrigramSimilarity(...) > x compiles to SIMILARITY(...) > x, which is not
    # an indexable expression -- it seq-scans 2.4M synonym rows, measured at
    # 4.49s for a single source value. `__trigram_similar` emits the `%`
    # operator, which is what gin_trgm_ops answers, so the GIN index does the
    # narrowing and similarity() only scores the handful that survive.
    # See _narrowing_filter for when `%>` replaces `%` here.
    #
    # The `%` must be applied to UPPER(col), not the raw column: both indexes
    # are on the uppercased expression, and querying the raw column silently
    # misses them -- the same raw-vs-UPPER mismatch that made concepts/search
    # ineffective (#262).
    #
    # `%` uses pg_trgm.similarity_threshold (0.3 by default), the same cut
    # MIN_TRIGRAM_SCORE applies -- the explicit filter stays so the constant
    # governs regardless of the session setting.
    narrowing = _narrowing_filter(query, domain_id)
    by_name = list(_name_matches(query, domain_id, limit, narrowing))
    wide = {'name_upper__trigram_similar': query}
    if not by_name and narrowing != wide:
        # A key word the names spell differently, "cutaneous" against "topical",
        # excludes everything. Nothing found means the key was wrong, not that
        # the vocabulary is empty, so pay for the wide search rather than return
        # less than the old code did.
        by_name = list(_name_matches(query, domain_id, limit, wide))

    # Synonyms are a separate index and a separate signal; merged by concept,
    # keeping whichever route scored higher.
    #
    # The domain-scoped table is the fast path (#1467). concept_synonym has no
    # domain or standing of its own, so its trigram index returns loosely similar
    # synonyms from every domain -- 30,000 to 160,000 rows per code on staging,
    # each scored, nearly all discarded by the join. suggest_synonym_term holds
    # the same text with the domain beside it and a partial index per domain.
    # Scores are identical: `term` is UPPER(concept_synonym_name).
    from omop_core.services import suggest_synonym_terms
    if suggest_synonym_terms.is_populated(domain_id):
        synonym_hits = (
            SuggestSynonymTerm.objects
            .filter(domain_id=domain_id, term__trigram_similar=query)
            .annotate(score=TrigramSimilarity('term', query))
            .filter(score__gt=MIN_TRIGRAM_SCORE)
            .values('concept_id')
            .annotate(score=Max('score'))
            .order_by('-score', 'concept_id')[:limit]
        )
    else:
        # No domain (ICD-10 searches all of them), or a table nobody has built:
        # search concept_synonym directly. Slow, but complete.
        synonym_hits = (
            ConceptSynonym.objects
            .filter(concept__standard_concept='S', concept__invalid_reason__isnull=True,
                    **({'concept__domain_id': domain_id} if domain_id else {}))
            .annotate(name_upper=Upper('concept_synonym_name'))
            .filter(name_upper__trigram_similar=query)
            .annotate(score=TrigramSimilarity(Upper('concept_synonym_name'), query))
            .filter(score__gt=MIN_TRIGRAM_SCORE)
            .values('concept_id')
            .annotate(score=Max('score'))
            .order_by('-score', 'concept_id')[:limit]
        )
    synonym_scores = {h['concept_id']: h['score'] + SYNONYM_BONUS for h in synonym_hits}

    merged = {}
    for concept in by_name:
        merged[concept.concept_id] = (concept, float(concept.score))
    if synonym_scores:
        for concept in Concept.objects.filter(
            concept_id__in=list(synonym_scores),
            standard_concept='S', invalid_reason__isnull=True,
            **({'domain_id': domain_id} if domain_id else {}),
        ):
            score = float(synonym_scores[concept.concept_id])
            if concept.concept_id not in merged or score > merged[concept.concept_id][1]:
                merged[concept.concept_id] = (concept, score)

    ranked = sorted(merged.values(),
                    key=lambda pair: (-pair[1], pair[0].concept_id))[:limit]
    return [
        {
            'concept_id': c.concept_id,
            'concept_name': c.concept_name,
            'concept_code': c.concept_code,
            'vocabulary_id': c.vocabulary_id,
            'concept_class_id': c.concept_class_id,
            'domain_id': c.domain_id,
            'standard_concept': c.standard_concept,
            'lexical_score': round(score, 3),
            'retrieval': STRATEGY_LEXICAL,
        }
        for c, score in ranked
    ]
