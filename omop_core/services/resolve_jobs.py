"""Dispatch bulk source-code resolution runs (Celery or inline).

Follows the same pattern as derivation_jobs.py and suggest_jobs.py:
Celery when CELERY_BROKER_URL is set, inline otherwise.
"""

from __future__ import annotations

import logging
import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from omop_core.mapping.code_resolution import resolve_person_source_codes
from omop_core.models import Person, SourceCodeResolveRun

logger = logging.getLogger(__name__)

BULK_RESOLVE_MAX_PERSONS = 500


def run_bulk_resolve(run_id: uuid.UUID) -> None:
    """Execute the bulk resolve, updating the run row as we go."""
    try:
        run = SourceCodeResolveRun.objects.get(id=run_id)
    except SourceCodeResolveRun.DoesNotExist:
        logger.error('SourceCodeResolveRun %s not found.', run_id)
        return

    run.state = SourceCodeResolveRun.RUNNING
    run.save(update_fields=['state'])

    person_ids = run.error_detail  # repurposed: stored as person_ids list at creation
    total_resolved = 0
    total_errors = 0
    error_details: list[dict] = []

    for i, pid in enumerate(person_ids):
        try:
            person = Person.objects.get(person_id=pid)
            result = resolve_person_source_codes(person)
            total_resolved += result['resolved']
        except Person.DoesNotExist:
            total_errors += 1
            error_details.append({'person_id': pid, 'error': 'Person not found'})
        except Exception as exc:
            total_errors += 1
            error_details.append({'person_id': pid, 'error': str(exc)})
            logger.exception('Error resolving person %s in run %s', pid, run_id)

        run.done = i + 1
        run.resolved = total_resolved
        run.errors = total_errors
        run.save(update_fields=['done', 'resolved', 'errors'])

    run.state = SourceCodeResolveRun.COMPLETED
    run.error_detail = error_details
    run.finished_at = timezone.now()
    run.save(update_fields=['state', 'error_detail', 'finished_at'])


def create_and_dispatch_resolve_run(person_ids: list[int], user=None) -> SourceCodeResolveRun:
    """Create a SourceCodeResolveRun and dispatch it (Celery or inline)."""
    run = SourceCodeResolveRun.objects.create(
        total=len(person_ids),
        created_by=user,
        # Store person_ids in error_detail temporarily (cleared on completion).
        error_detail=person_ids,
    )

    if getattr(settings, 'CELERY_BROKER_URL', ''):
        from omop_core.tasks import bulk_resolve_source_codes_task
        transaction.on_commit(
            lambda: bulk_resolve_source_codes_task.delay(str(run.id))
        )
    else:
        # Inline: run synchronously before returning.
        run_bulk_resolve(run.id)

    return run
