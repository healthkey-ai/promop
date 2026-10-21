"""Move stored clinical rows onto an approved mapping's destination, off the request.

Approving a mapping, or re-pointing an approved one, used to rewrite every
stored clinical row in the save request. On a common code that is minutes of
UPDATEs, and the curator waited for all of it.

The save now writes the mapping and records the destinations whose rows still
have to move in ``deferred_repoint_concept_ids``; a job moves them. That column
is the queue, not the job: a lost or duplicated task, or a worker restart, is
answered by the next run reading the same column, and nothing is lost between
the save and the worker. It is the same column a patient-scoped save already
uses, so Generate OMOP sees rows still waiting for this job too.

Celery when ``CELERY_BROKER_URL`` is set, inline after commit otherwise, the
rule ``derivation_jobs`` and ``suggest_jobs`` follow.
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection, transaction

from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID, repoint_clinical_rows
from omop_core.models import SourceCodeConceptMapping

logger = logging.getLogger(__name__)

# A save that lands while a run is moving rows adds destinations the run did
# not read. The run picks those up before it stops, rather than leaving them
# for a task that may already have been dropped as a duplicate.
MAX_PASSES = 5
# Arbitrary, distinct from any other advisory lock key in this codebase.
_LOCK_NAMESPACE = 19140021


def queue_repoint(mapping, concept_ids):
    """Record destinations to move off and dispatch the job after commit.

    Must be called inside the transaction that saved the mapping, so the job
    can never run before the destinations it is meant to read are visible.
    """
    if not queue_repoints([(mapping, concept_ids)]):
        return None
    return {'queued': True, 'from_concept_ids': mapping.deferred_repoint_concept_ids}


def _after_commit(func):
    # robust: the mapping is already committed, and the column keeps the work
    # for the next run, so a failed dispatch must not turn the save into a 500.
    transaction.on_commit(func, robust=True)


def queue_repoints(mappings_with_sources):
    """``queue_repoint`` for many mappings: one write, one task per mapping.

    One task each rather than one for the batch, so a large upload is not held
    to a single task's time limit and the worker can run them side by side.
    """
    queued = []
    for mapping, concept_ids in mappings_with_sources:
        pending = sorted((set(mapping.deferred_repoint_concept_ids or []) | set(concept_ids))
                         - {mapping.target_concept_id})
        if pending:
            mapping.deferred_repoint_concept_ids = pending
            queued.append(mapping)
    if not queued:
        return 0
    SourceCodeConceptMapping.objects.bulk_update(queued, ['deferred_repoint_concept_ids'], batch_size=1_000)
    ids = [mapping.pk for mapping in queued]
    _after_commit(lambda: _dispatch_each(ids))
    return len(ids)


def _dispatch_each(ids):
    # One mapping that fails inline must not stop the rest; its work stays queued.
    for pk in ids:
        try:
            dispatch_repoint(pk)
        except Exception:  # noqa: BLE001
            logger.exception('Re-point of mapping %s failed; it stays queued.', pk)


def dispatch_repoint(mapping_id):
    if getattr(settings, 'CELERY_BROKER_URL', ''):
        try:
            from omop_core.tasks import repoint_mapping_rows_task
            repoint_mapping_rows_task.delay(mapping_id)
            return
        except Exception:  # noqa: BLE001 - a queue outage must not strand the rows
            logger.warning('Could not queue the re-point of mapping %s; running inline.', mapping_id)
    run_repoint(mapping_id)


def _advisory_lock(mapping_id):
    """One run per mapping. Session-level so it spans the per-sweep transactions."""
    if connection.vendor != 'postgresql':
        return True
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [_LOCK_NAMESPACE, mapping_id])
        return cursor.fetchone()[0]


def _advisory_unlock(mapping_id):
    if connection.vendor == 'postgresql':
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [_LOCK_NAMESPACE, mapping_id])


def run_repoint(mapping_id):
    """Move this mapping's waiting rows. Returns the summed sweep counts.

    A run that finds another run holding the mapping returns at once: that run
    re-reads the queue before it stops, so the destinations are not lost.
    """
    totals = {'rows_updated': 0, 'persons_marked_stale': 0, 'rows_collapsed': 0}
    if not _advisory_lock(mapping_id):
        logger.info('Re-point of mapping %s is already running.', mapping_id)
        return totals
    persons, moved_ids = set(), set()
    try:
        for _ in range(MAX_PASSES):
            mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id).first()
            # Un-approved, or pointed at nothing, since the save: the rows stay
            # where they are, and the queue stays for the next approval.
            if (mapping is None or mapping.status != 'approved'
                    or mapping.target_concept_id is None
                    or not mapping.deferred_repoint_concept_ids):
                break
            target = mapping.target_concept_id
            sources = [c for c in mapping.deferred_repoint_concept_ids if c != target]
            for old_concept_id in sources:
                outcome = repoint_clinical_rows(
                    mapping=mapping, old_concept_id=old_concept_id, new_concept_id=target,
                    # The concept-0 sweep reaches rows this mapping's own import
                    # never wrote, so it matches the code only -- see
                    # _source_value_match.
                    match_description=old_concept_id != NO_MATCHING_CONCEPT_ID,
                )
                persons |= outcome.pop('person_ids', set())
                moved_ids.add(old_concept_id)
                for key in ('rows_updated', 'rows_collapsed'):
                    totals[key] += outcome[key]
            with transaction.atomic():
                current = SourceCodeConceptMapping.objects.select_for_update().filter(pk=mapping_id).first()
                if current is None:
                    break
                if current.target_concept_id == target:
                    # Only what this pass moved. A save during the pass may have
                    # added more, which the next pass reads.
                    current.deferred_repoint_concept_ids = [
                        c for c in current.deferred_repoint_concept_ids if c not in sources and c != target
                    ]
                    current.save(update_fields=['deferred_repoint_concept_ids'])
                # Re-pointed again mid-run: the rows this pass moved now sit on
                # the old target, which that save queued, so the next pass
                # carries them on to the new one.
    finally:
        _advisory_unlock(mapping_id)
    totals['persons_marked_stale'] = len(persons)
    logger.info('Re-point of mapping %s finished: %s.', mapping_id, totals)
    _redispatch_late_arrivals(mapping_id, moved_ids)
    return totals


def _redispatch_late_arrivals(mapping_id, moved_ids):
    """Catch a save that committed after this run's last read.

    That save's own task found the lock held and returned, trusting this run to
    pick its destinations up. Only destinations this run never moved count, so
    a run cannot keep re-dispatching itself over the same work.
    """
    mapping = SourceCodeConceptMapping.objects.filter(pk=mapping_id, status='approved').first()
    if mapping is None:
        return
    if set(mapping.deferred_repoint_concept_ids) - moved_ids - {mapping.target_concept_id}:
        dispatch_repoint(mapping_id)
