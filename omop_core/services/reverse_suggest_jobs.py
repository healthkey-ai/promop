"""Persist bounded reverse retrieval previews using the existing job dispatcher."""
import logging

from django.db import transaction
from django.utils import timezone

from omop_core.models import SourceCodeConceptMapping, SuggestRun
from omop_core.mapping.reverse_suggestions import (
    record_declines, reverse_rank_candidate, reverse_retrieval_pool,
)
from omop_core.services.concept_to_code import concept_payload, standard_destinations
from omop_core.services.source_vocabularies import DOMAIN_TO_TABLE, VOCABULARY_OID_ALIASES

logger = logging.getLogger(__name__)


def _may_write_variants(mapping, user):
    """The same rule the variant endpoint applies to its caller: the run's
    creator must administer the mapping's organization and nobody else may
    hold its edit lock. Other hospitals' codes are listed, never written."""
    from patient_portal.api import views

    if user is None or not views._can_administer_code_mapping(user, mapping):
        return False
    try:
        views._check_mapping_lock(mapping, user)
    except views.MappingLocked:
        return False
    return True


def _auto_propose_candidate(candidate, concept, user):
    """Create a proposed SCCM row for a high-confidence candidate (#2269).

    Returns the updated candidate dict with ``mapping_id`` and ``auto_proposed``
    set, or the original candidate unchanged if the source already has a mapping
    or the catalog entry is stale.
    """
    from omop_core.mapping.reverse_catalog import refresh_catalog_candidate
    from patient_portal.api.views import _upsert_source_code_mapping

    if candidate.get('mapping_id') is not None or candidate.get('declined'):
        return candidate
    fresh = refresh_catalog_candidate(candidate, concept)
    if fresh is None:
        return candidate
    aliases = [fresh['source_vocabulary_id'], *[alias for alias, canonical in VOCABULARY_OID_ALIASES.items()
                                                 if canonical == fresh['source_vocabulary_id']]]
    if SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id__in=aliases, source_code=fresh['source_code'],
    ).exists():
        return candidate
    with transaction.atomic():
        row, _ = _upsert_source_code_mapping(concept, {
            'source_vocabulary_id': fresh['source_vocabulary_id'],
            'source_code': fresh['source_code'],
            'source_code_description': fresh['source_code_description'][:255],
            **({'notes': f"Source vocabulary label: {fresh['source_code_description']}"}
               if len(fresh['source_code_description']) > 255 else {}),
            'domain_id': concept.domain_id, 'omop_table': DOMAIN_TO_TABLE[concept.domain_id],
            'status': 'proposed',
        }, user)
        if candidate.get('confidence') is not None:
            row.suggestion_confidence = candidate['confidence']
            row.save(update_fields=['suggestion_confidence'])
    return {**candidate, 'mapping_id': row.pk, 'auto_proposed': True}


def _with_variant_suggestions(candidate, params, run, budget):
    """Fill in a flagged candidate's axis variants with proposed LOINCs (#2012).

    A source code whose units imply several LOINC axis sets needs one
    destination per variant, not just the concept this run is for. Each
    variant without a destination is suggested with the run's ranker and
    stored as proposed; the candidate carries every variant so the curator can
    approve them from the queue. Curator review (no ranker) only lists them.

    ``budget`` is the run's remaining variant allowance: a run suggests at most
    its own ``limit`` of variants on top of its candidates, so it costs at most
    twice what it was given, whichever dispatcher runs it.
    """
    from omop_core.mapping import axis_variants
    from omop_core.models import SourceCodeConceptMapping

    if not candidate.get('mapping_id') or (candidate.get('axis_variant_count') or 0) < axis_variants.FLAG_THRESHOLD:
        return candidate
    mapping = SourceCodeConceptMapping.objects.filter(pk=candidate['mapping_id']).first()
    if mapping is None:
        return candidate
    if params['ranking_model'] != 'none' and budget['left'] > 0 and _may_write_variants(mapping, run.created_by):
        try:
            results = axis_variants.suggest_destinations(
                mapping, strategies=params['strategies'], ranking_model=params['ranking_model'],
                user=run.created_by, limit=min(axis_variants.SUGGEST_MAX, budget['left']))
            budget['left'] -= len(results)
        except Exception:  # the candidate itself is still a result
            logger.exception('Axis variant suggestion failed for mapping %s', mapping.pk)
    return {**candidate, 'axis_variants': axis_variants.serialize_all(mapping)}


def execute_reverse_run(run_id, params):
    runs = SuggestRun.objects.filter(pk=run_id, direction='reverse')
    # A redelivered Celery task must not restart a completed/running preview.
    if not runs.filter(state=SuggestRun.QUEUED).update(state=SuggestRun.RUNNING):
        return
    run = runs.first()
    budget = {'left': params['limit']}
    threshold = params.get('auto_propose_threshold')
    events = []
    done = 0
    proposed = 0
    try:
        for concept_id in params['concept_ids']:
            concept = standard_destinations().get(pk=concept_id)
            candidates = reverse_retrieval_pool(
                concept, strategies=params['strategies'], limit=params['limit'],
                include_zero_seen=params['include_zero_seen'],
                include_declined=params.get('include_declined', False),
                exclude_keys=params.get('exclude_keys', ()),
                timeout_ms=params['retrieval_timeout_ms'],
            )
            event = {'concept': concept_payload(concept), 'candidates': candidates}
            events.append(event)
            runs.update(activity=events, retrieved=len(events))
            for index, candidate in enumerate(candidates):
                ranked = reverse_rank_candidate(concept, candidate, params['ranking_model'])
                ranked = _with_variant_suggestions(ranked, params, run, budget)
                # Auto-propose candidates above the confidence threshold (#2269).
                if (threshold is not None
                        and isinstance(ranked.get('confidence'), (int, float))
                        and ranked['confidence'] >= threshold):
                    try:
                        ranked = _auto_propose_candidate(ranked, concept, run.created_by)
                        if ranked.get('auto_proposed'):
                            proposed += 1
                    except Exception:
                        logger.exception('Auto-propose failed for %s/%s',
                                         ranked.get('source_vocabulary_id'), ranked.get('source_code'))
                event['candidates'][index] = ranked
                runs.update(activity=events)
            # What the ranker declined this run is remembered, so the next run
            # for this concept moves on to sources this one did not reach (#2034).
            event['declined'] = record_declines(
                concept, event['candidates'], ranking_model=params['ranking_model'], run=run)
            runs.update(activity=events)
            done += 1
            runs.update(done=done)
        runs.update(state=SuggestRun.SUCCESS, finished_at=timezone.now(), destinations=proposed)
    except Exception:  # retain partial previews, never overwrite source mappings
        logger.exception('Reverse suggestion run failed: %s', run_id)
        runs.update(state=SuggestRun.FAILURE, finished_at=timezone.now(),
                    error='Source-code search failed. Partial candidates remain available; retry the search.')
