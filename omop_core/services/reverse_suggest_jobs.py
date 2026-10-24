"""Persist bounded reverse retrieval previews using the existing job dispatcher."""
import logging

from django.utils import timezone

from omop_core.models import SuggestRun
from omop_core.mapping.reverse_suggestions import reverse_rank_candidate, reverse_retrieval_pool
from omop_core.services.concept_to_code import concept_payload, standard_destinations

logger = logging.getLogger(__name__)


def _with_variant_suggestions(candidate, params):
    """Fill in a flagged candidate's axis variants with proposed LOINCs (#2012).

    A source code whose units imply several LOINC axis sets needs one
    destination per variant, not just the concept this run is for. Each
    variant without a destination is suggested with the run's ranker and
    stored as proposed; the candidate carries every variant so the curator can
    approve them from the queue. Curator review (no ranker) only lists them.
    """
    from omop_core.mapping import axis_variants
    from omop_core.models import SourceCodeConceptMapping

    if not candidate.get('mapping_id') or (candidate.get('axis_variant_count') or 0) < axis_variants.FLAG_THRESHOLD:
        return candidate
    mapping = SourceCodeConceptMapping.objects.filter(pk=candidate['mapping_id']).first()
    if mapping is None:
        return candidate
    if params['ranking_model'] != 'none':
        try:
            axis_variants.suggest_destinations(
                mapping, strategies=params['strategies'], ranking_model=params['ranking_model'],
                user=None)
        except Exception:  # the candidate itself is still a result
            logger.exception('Axis variant suggestion failed for mapping %s', mapping.pk)
    variants = mapping.axis_variants.select_related('target_concept').order_by('-occurrence_count', 'axis_key')
    return {**candidate, 'axis_variants': [axis_variants.serialize(v) for v in variants]}


def execute_reverse_run(run_id, params):
    runs = SuggestRun.objects.filter(pk=run_id, direction='reverse')
    # A redelivered Celery task must not restart a completed/running preview.
    if not runs.filter(state=SuggestRun.QUEUED).update(state=SuggestRun.RUNNING):
        return
    events = []
    done = 0
    try:
        for concept_id in params['concept_ids']:
            concept = standard_destinations().get(pk=concept_id)
            candidates = reverse_retrieval_pool(
                concept, strategies=params['strategies'], limit=params['limit'],
                include_zero_seen=params['include_zero_seen'],
                timeout_ms=params['retrieval_timeout_ms'],
            )
            event = {'concept': concept_payload(concept), 'candidates': candidates}
            events.append(event)
            runs.update(activity=events, retrieved=len(events))
            for index, candidate in enumerate(candidates):
                ranked = reverse_rank_candidate(concept, candidate, params['ranking_model'])
                event['candidates'][index] = _with_variant_suggestions(ranked, params)
                runs.update(activity=events)
            done += 1
            runs.update(done=done)
        runs.update(state=SuggestRun.SUCCESS, finished_at=timezone.now())
    except Exception:  # retain partial previews, never overwrite source mappings
        logger.exception('Reverse suggestion run failed: %s', run_id)
        runs.update(state=SuggestRun.FAILURE, finished_at=timezone.now(),
                    error='Source-code search failed. Partial candidates remain available; retry the search.')
