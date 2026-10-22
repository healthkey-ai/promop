"""Persist bounded reverse retrieval previews using the existing job dispatcher."""
import logging

from django.utils import timezone

from omop_core.models import SuggestRun
from omop_core.mapping.reverse_suggestions import reverse_rank_candidate, reverse_retrieval_pool
from omop_core.services.concept_to_code import concept_payload, standard_destinations

logger = logging.getLogger(__name__)


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
                event['candidates'][index] = reverse_rank_candidate(concept, candidate, params['ranking_model'])
                runs.update(activity=events)
            done += 1
            runs.update(done=done)
        runs.update(state=SuggestRun.SUCCESS, finished_at=timezone.now())
    except Exception:  # retain partial previews, never overwrite source mappings
        logger.exception('Reverse suggestion run failed: %s', run_id)
        runs.update(state=SuggestRun.FAILURE, finished_at=timezone.now(),
                    error='Source-code search failed. Partial candidates remain available; retry the search.')
