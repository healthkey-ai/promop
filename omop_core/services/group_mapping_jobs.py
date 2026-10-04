"""One label decision fanned out safely to its still-proposed source codes."""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from omop_core.models import Concept, SourceCodeConceptMapping, SuggestRun
from omop_core.services.source_labels import normalise


NOISE_LABELS = frozenset({
    'abnormalflag', 'addendum', 'cancelled', 'comment', 'comments', 'disclaimer',
    'exceptionstowdl', 'finalresult', 'impression', 'inprogress', 'laboratory',
    'method', 'methodology', 'nodata', 'note', 'orderedby', 'pending',
    'performedby', 'pleasenote', 'received', 'report', 'reportstatus', 'result',
    'reviewedby', 'seenote', 'seereport', 'specimen', 'specimensource', 'summary',
    'text', 'transcription', 'wdl', 'within defined limits', 'wnl',
})
# This is the stored key, so normalize the one spaced phrase too.
NOISE_LABELS = frozenset(filter(None, (normalise(value) for value in NOISE_LABELS)))


def deterministic_proposal(label: str, domain_id: str):
    """Return a no-model proposal, or ``None`` when ranking is still needed."""
    key = normalise(label)
    if key in NOISE_LABELS:
        return {'action': 'reject', 'strategy': 'noise', 'note': (
            'Narrative/result metadata label; proposed for curator-confirmed rejection.'
        )}

    from omop_core.mapping.suggestions import lexical_candidates
    exact = {
        candidate['concept_id']: candidate
        for candidate in lexical_candidates(label, domain_id, limit=50)
        if candidate.get('lexical_score', 0) >= 0.999
        or normalise(candidate.get('concept_name')) == key
    }
    if len(exact) == 1:
        return {
            'concept': next(iter(exact.values())), 'strategy': 'exact',
            'note': 'Unique exact standard concept/name-or-synonym match; no ranker call used.',
        }
    return None


def _write_group_suggestion(mapping_ids, proposal, *, run, actor):
    chosen = proposal.get('concept')
    concept = Concept.objects.filter(concept_id=chosen['concept_id']).first() if chosen else None
    done = destinations = 0
    refreshed_at = timezone.now()
    refresh_interval = timedelta(seconds=max(15, settings.MAPPING_LOCK_TIMEOUT_MINUTES * 30))
    for position, mapping_id in enumerate(mapping_ids):
        if timezone.now() - refreshed_at >= refresh_interval:
            SourceCodeConceptMapping.objects.filter(
                pk__in=mapping_ids[position:], locked_by=actor,
            ).update(locked_at=timezone.now())
            refreshed_at = timezone.now()
        with transaction.atomic():
            mapping = SourceCodeConceptMapping.objects.select_for_update().filter(pk=mapping_id).first()
            if mapping is None or mapping.status != 'proposed':
                continue
            if mapping.locked_by_id != actor.pk:
                raise RuntimeError('A group member lock changed while Suggest was running.')
            if proposal.get('action') == 'reject':
                mapping.suggested_action = 'reject'
                mapping.suggest_strategy = proposal['strategy']
                mapping.notes = mapping.notes or proposal['note']
                mapping.last_suggest_attempt = 'group-noise-v1'
                mapping.save(update_fields=[
                    'suggested_action', 'suggest_strategy', 'notes',
                    'last_suggest_attempt', 'updated_at',
                ])
            elif concept is not None and mapping.target_concept_id is None:
                # Group controls can also appear on a non-vendor rollup. Keep
                # the same Athena-duplicate protection as ordinary Suggest,
                # without paying that lookup for thousands of hospital-local
                # identities Athena cannot possibly supply.
                from omop_core.services import source_vocabularies
                from omop_core.services.athena_mapping_guard import athena_supplies_mapping
                if (
                    not source_vocabularies.hospital_vendor(mapping.source_vocabulary_id)
                    and athena_supplies_mapping(
                        mapping.source_vocabulary_id, mapping.source_code, concept.pk,
                    )
                ):
                    continue
                from omop_core.mapping.suggestions import SUGGESTION_MODEL_VERSION, SUGGESTION_PROVENANCE
                mapping.target_concept = concept
                mapping.suggested_target_concept = concept
                mapping.destination_vocabulary_id = concept.vocabulary_id
                mapping.origin_system = SUGGESTION_PROVENANCE
                mapping.suggestion_model_version = SUGGESTION_MODEL_VERSION
                # None for an exact match: no ranker was asked.
                mapping.suggestion_confidence = proposal.get('confidence')
                mapping.last_suggest_attempt = f'{SUGGESTION_MODEL_VERSION}-group'
                mapping.suggest_strategy = proposal['strategy']
                mapping.suggested_action = ''
                mapping.notes = mapping.notes or proposal['note']
                mapping.save(update_fields=[
                    'target_concept', 'suggested_target_concept',
                    'destination_vocabulary_id', 'origin_system',
                    'suggestion_model_version', 'suggestion_confidence', 'last_suggest_attempt',
                    'suggest_strategy', 'suggested_action', 'notes', 'updated_at',
                ])
                destinations += 1
            done += 1
            SuggestRun.objects.filter(pk=run.pk).update(done=done, destinations=destinations)
    return done, destinations


def _apply_group_action(mapping_ids, action, destination_id, *, run, actor, selected_targets):
    from patient_portal.api.views import _upsert_source_code_mapping

    concept = Concept.objects.filter(concept_id=destination_id).first() if destination_id else None
    done = 0
    refreshed_at = timezone.now()
    refresh_interval = timedelta(seconds=max(15, settings.MAPPING_LOCK_TIMEOUT_MINUTES * 30))
    for position, mapping_id in enumerate(mapping_ids):
        # A 2,557-member approval can outlive the ordinary 15-minute edit lock.
        # Refresh the batch periodically so another curator cannot enter halfway
        # through the per-row repoint loop.
        if timezone.now() - refreshed_at >= refresh_interval:
            SourceCodeConceptMapping.objects.filter(
                pk__in=mapping_ids[position:], locked_by=actor,
            ).update(locked_at=timezone.now())
            refreshed_at = timezone.now()
        with transaction.atomic():
            mapping = (SourceCodeConceptMapping.objects.select_for_update(of=('self',))
                       .select_related('organization').filter(pk=mapping_id).first())
            if mapping is None or mapping.status != 'proposed':
                continue
            if mapping.locked_by_id != actor.pk:
                raise RuntimeError('A group member lock changed while the decision was running.')
            if mapping.target_concept_id != selected_targets.get(str(mapping.pk)):
                raise RuntimeError(
                    'A group member destination changed while the decision was running.'
                )
            _upsert_source_code_mapping(
                concept if action == 'approve' else mapping.target_concept,
                {'status': 'approved' if action == 'approve' else 'rejected'},
                actor, mapping=mapping,
            )
            done += 1
            SuggestRun.objects.filter(pk=run.pk).update(
                done=done, destinations=done if action == 'approve' else 0,
            )
    return done


def execute_group_run(run_id: str, params: dict) -> None:
    """Execute a pre-locked group action and persist progress for polling."""
    run = SuggestRun.objects.filter(pk=run_id).first()
    if run is None:
        return
    rows = list(params.get('_locked_mapping_ids') or [])
    actor = get_user_model().objects.filter(pk=params.get('actor_id')).first()
    SuggestRun.objects.filter(pk=run.pk).update(state=SuggestRun.RUNNING)
    try:
        if actor is None:
            raise ValueError('The curator who started this group job no longer exists.')
        SourceCodeConceptMapping.objects.filter(pk__in=rows, locked_by=actor).update(
            locked_at=timezone.now(),
        )
        if params['mode'] == 'group-action':
            _apply_group_action(
                rows, params['action'], params.get('destination_concept_id'),
                run=run, actor=actor,
                selected_targets=params.get('_selected_target_ids') or {},
            )
        else:
            representative = (SourceCodeConceptMapping.objects
                              .filter(pk__in=rows).order_by('-occurrence_count', 'pk').first())
            if representative is None:
                proposal = {'note': 'No still-proposed group members remained.', 'strategy': ''}
            else:
                label = representative.source_code_description
                proposal = deterministic_proposal(label, representative.domain_id)
                if proposal is None:
                    from omop_core.mapping.suggestions import chosen_confidence, suggest_one_mapping
                    result = suggest_one_mapping(
                        representative.source_code, representative.source_vocabulary_id,
                        representative.omop_table, source_description=label,
                        strategies=params['strategies'], lexical_limit=params['lexical_limit'],
                        ranking_model=params['ranking_model'],
                    )
                    proposal = {
                        'concept': result.get('suggested'),
                        'strategy': result.get('strategy_used') or '',
                        'note': result.get('note') or 'No candidate concept found.',
                        'confidence': chosen_confidence(result.get('suggested'), result.get('alternatives')),
                    }
                SuggestRun.objects.filter(pk=run.pk).update(retrieved=run.total)
                _write_group_suggestion(rows, proposal, run=run, actor=actor)
            events = [{'at': timezone.now().isoformat(), 'stage': 'result', **proposal}]
            SuggestRun.objects.filter(pk=run.pk).update(activity=events)
        SuggestRun.objects.filter(pk=run.pk).update(
            state=SuggestRun.SUCCESS, finished_at=timezone.now(), retrieved=run.total,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced through the poll row
        SuggestRun.objects.filter(pk=run.pk).update(
            state=SuggestRun.FAILURE, error=str(exc)[:2000], finished_at=timezone.now(),
        )
    finally:
        SourceCodeConceptMapping.objects.filter(pk__in=rows, locked_by=actor).update(
            locked_by=None, locked_at=None,
        )
