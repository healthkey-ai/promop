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


def run_bulk_resolve(run_id: uuid.UUID, person_ids: list[int] | None = None) -> None:
    """Execute the bulk resolve, updating the run row as we go."""
    try:
        run = SourceCodeResolveRun.objects.get(id=run_id)
    except SourceCodeResolveRun.DoesNotExist:
        logger.error('SourceCodeResolveRun %s not found.', run_id)
        return

    if person_ids is None:
        logger.error('No person_ids provided for run %s.', run_id)
        run.state = SourceCodeResolveRun.FAILED
        run.error_detail = [{'error': 'No person_ids provided'}]
        run.finished_at = timezone.now()
        run.save(update_fields=['state', 'error_detail', 'finished_at'])
        return

    run.state = SourceCodeResolveRun.RUNNING
    run.save(update_fields=['state'])

    total_resolved = 0
    total_created = 0
    total_skipped = 0
    total_already_resolved = 0
    total_errors = 0
    error_details: list[dict] = []
    all_code_details: list[dict] = []

    MAX_CODE_DETAILS = 500

    try:
        for i, pid in enumerate(person_ids):
            try:
                person = Person.objects.get(person_id=pid)
                result = resolve_person_source_codes(person)
                total_resolved += result['resolved']
                total_created += result.get('created', 0)
                total_skipped += result['skipped']
                total_already_resolved += result['already_resolved']
                all_code_details.extend(result.get('code_details', []))
                if len(all_code_details) > MAX_CODE_DETAILS:
                    all_code_details = all_code_details[:MAX_CODE_DETAILS]
            except Person.DoesNotExist:
                total_errors += 1
                error_details.append({'person_id': pid, 'error': 'Person not found'})
            except Exception as exc:
                total_errors += 1
                error_details.append({'person_id': pid, 'error': str(exc)})
                logger.exception('Error resolving person %s in run %s', pid, run_id)

            run.done = i + 1
            run.resolved = total_resolved
            run.created = total_created
            run.skipped = total_skipped
            run.already_resolved = total_already_resolved
            run.errors = total_errors
            run.save(update_fields=['done', 'resolved', 'created', 'skipped', 'already_resolved', 'errors'])

        run.state = SourceCodeResolveRun.COMPLETED
    except BaseException:
        run.state = SourceCodeResolveRun.FAILED
        logger.exception('Bulk resolve run %s crashed', run_id)
        raise
    finally:
        run.error_detail = error_details
        run.code_details = all_code_details
        run.finished_at = timezone.now()
        run.save(update_fields=['state', 'error_detail', 'code_details', 'finished_at'])


def create_and_dispatch_resolve_run(person_ids: list[int], user=None) -> SourceCodeResolveRun:
    """Create a SourceCodeResolveRun and dispatch it (Celery or inline)."""
    run = SourceCodeResolveRun.objects.create(
        total=len(person_ids),
        created_by=user,
    )

    if getattr(settings, 'CELERY_BROKER_URL', ''):
        from omop_core.tasks import bulk_resolve_source_codes_task
        transaction.on_commit(
            lambda: bulk_resolve_source_codes_task.delay(str(run.id), person_ids)
        )
    else:
        # Inline: run synchronously before returning.
        run_bulk_resolve(run.id, person_ids)

    return run
