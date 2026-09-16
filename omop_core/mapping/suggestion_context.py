"""Bounded vocabulary evidence prepared before the network-only ranking phase."""
from collections import defaultdict
import logging

from django.contrib.postgres.search import TrigramSimilarity
from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Value, When, Window
from django.db.models.functions import RowNumber, Upper
from django.utils import timezone

from omop_core.models import ConceptRelationship, ConceptSynonym

logger = logging.getLogger(__name__)
MAX_EVIDENCE_ITEMS = 3
MAX_CONTEXT_TEXT = 512


def build_source_context(*, source_code, vocabulary_id, description,
                         source_concept, umls_name, domain_id, omop_table):
    """Keep independently sourced labels, including disagreements between them."""
    concept = None if source_concept is None else {
        'concept_id': source_concept.pk,
        'name': source_concept.concept_name,
        'vocabulary_id': source_concept.vocabulary_id,
        'code': source_concept.concept_code,
        'domain_id': source_concept.domain_id,
        'invalid_reason': source_concept.invalid_reason,
    }
    return {
        'code': source_code,
        'vocabulary_id': vocabulary_id or None,
        'original_description': (description or '')[:MAX_CONTEXT_TEXT],
        'loaded_source_concept': concept,
        'umls_preferred_name': (umls_name or '')[:MAX_CONTEXT_TEXT],
        'expected_domain': domain_id or None,
        'omop_table': omop_table or None,
    }


def enrich_candidates(candidates, query, source_concept_id, *, min_similarity):
    """Attach evidence without changing selection or order.

    Window limits avoid a synonym-rich concept starving other candidates of
    context. Savepoints prevent optional evidence errors aborting the caller.
    No clinical records or previous machine suggestions are used as evidence.
    """
    if not candidates:
        return candidates
    ids = [c['concept_id'] for c in candidates]
    synonyms, relationships = defaultdict(list), defaultdict(list)
    query = (query or '').strip().upper()
    if len(query) >= 3:
        try:
            with transaction.atomic():
                hits = list(ConceptSynonym.objects.filter(concept_id__in=ids)
                    .annotate(similarity=TrigramSimilarity(Upper('concept_synonym_name'), query))
                    .filter(similarity__gt=min_similarity)
                    .annotate(position=Window(
                        expression=RowNumber(), partition_by=[F('concept_id')],
                        order_by=[F('similarity').desc(), F('concept_synonym_name').asc(), F('pk').asc()],
                    ))
                    .filter(position__lte=MAX_EVIDENCE_ITEMS)
                    .order_by('concept_id', 'position')
                    .values('concept_id', 'concept_synonym_name', 'language_concept_id'))
            for hit in hits:
                synonyms[hit['concept_id']].append({
                    'text': hit['concept_synonym_name'][:MAX_CONTEXT_TEXT],
                    'language_concept_id': hit['language_concept_id'],
                })
        except Exception:  # noqa: BLE001 - optional context must not discard candidates
            logger.warning('Candidate synonym context unavailable.', exc_info=True)

    if source_concept_id is not None:
        try:
            with transaction.atomic():
                today = timezone.now().date()
                rows = list(ConceptRelationship.objects.filter(
                    Q(concept_1_id=source_concept_id, concept_2_id__in=ids)
                    | Q(concept_2_id=source_concept_id, concept_1_id__in=ids),
                    invalid_reason__isnull=True,
                    valid_start_date__lte=today, valid_end_date__gte=today,
                ).annotate(
                    peer_id=Case(When(concept_1_id=source_concept_id, then=F('concept_2_id')),
                                 default=F('concept_1_id'), output_field=IntegerField()),
                    priority=Case(
                        When(relationship_id='Maps to', then=Value(0)),
                        When(relationship_id='Mapped from', then=Value(1)),
                        default=Value(2), output_field=IntegerField(),
                    ),
                ).annotate(position=Window(
                    expression=RowNumber(), partition_by=[F('peer_id')],
                    order_by=[F('priority').asc(), F('relationship_id').asc(), F('pk').asc()],
                )).filter(position__lte=MAX_EVIDENCE_ITEMS)
                  .order_by('peer_id', 'position')
                  .values('peer_id', 'concept_1_id', 'concept_2_id', 'relationship_id',
                          'relationship__relationship_name'))
            for row in rows:
                relationships[row['peer_id']].append({
                    'from_concept_id': row['concept_1_id'],
                    'to_concept_id': row['concept_2_id'],
                    'relationship_id': row['relationship_id'],
                    'relationship_name': row['relationship__relationship_name'],
                })
        except Exception:  # noqa: BLE001 - optional context must not discard candidates
            logger.warning('Candidate relationship context unavailable.', exc_info=True)

    return [{**c, 'matched_synonyms': synonyms[c['concept_id']],
             'source_relationships': relationships[c['concept_id']]} for c in candidates]


def candidate_context(candidate):
    """Only evidence reaches the LLM; retrieval scores are not clinical confidence."""
    return {
        'concept_id': candidate['concept_id'],
        'name': candidate['concept_name'],
        'code': candidate['concept_code'],
        'vocabulary_id': candidate['vocabulary_id'],
        'domain_id': candidate.get('domain_id'),
        'concept_class_id': candidate['concept_class_id'],
        'matched_synonyms': candidate.get('matched_synonyms', [])[:MAX_EVIDENCE_ITEMS],
        'shared_umls_cuis': candidate.get('umls_cuis', [])[:MAX_EVIDENCE_ITEMS],
        'source_relationships': candidate.get('source_relationships', [])[:MAX_EVIDENCE_ITEMS],
        'generated_search_query': candidate.get('generated_search_query'),
    }
