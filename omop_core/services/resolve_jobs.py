"""Dispatch bulk source-code resolution runs (Celery or inline).

Follows the same pattern as derivation_jobs.py and suggest_jobs.py:
Celery when CELERY_BROKER_URL is set, inline otherwise.

A run for the Patients list resolves each selected patient and leaves the
record to the resolver, which queues a derivation only for a patient whose
rows it changed. A run started from one patient's Source Codes tab
(``refresh_record``, #2822) derives that patient's record itself, after the
resolve and whether or not a row changed, so the run is finished only when the
record is: the tab polls one thing, and a record an earlier save left stale is
brought up to date.
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

# ``step`` on an ``error_detail`` entry for a record that could not be derived.
# The rows the resolve wrote are committed by then, so it is reported apart
# from a resolve that failed.
RECORD_STEP = 'record'


def record_refresh_errors(run: SourceCodeResolveRun) -> int:
    """How many of *run*'s patients had their rows resolved and their record not derived."""
    return sum(1 for entry in run.error_detail or []
               if isinstance(entry, dict) and entry.get('step') == RECORD_STEP)


def _refresh_record(person: Person) -> None:
    """Derive *person*'s record now, in the run."""
    if getattr(settings, 'CELERY_BROKER_URL', ''):
        # On a worker: bounded by the task's time limit, as a queued derivation is.
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)
        return
    # In the request: the inline dispatcher derives before it returns, under its
    # statement timeout, and lets a failure propagate.
    from omop_core.services.derivation_jobs import get_dispatcher
    get_dispatcher().dispatch(person)


def run_bulk_resolve(run_id: uuid.UUID, person_ids: list[int] | None = None,
                     refresh_record: bool = False) -> None:
    """Execute the bulk resolve, updating the run row as we go.

    With *refresh_record*, each patient's record is derived here once their
    rows are resolved, also when the resolve changed none or stopped partway
    (what it committed is still to be derived from). A record that cannot be
    derived counts as an error with ``step: 'record'``; the rows stay resolved.
    """
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
        run.save(update_fields=['state', 'error_detail', 'finished_at', 'updated_at'])
        return

    # A run closed as stale whose task arrives late is reopened, as a sweep chunk does.
    run.state = SourceCodeResolveRun.RUNNING
    run.finished_at = None
    run.save(update_fields=['state', 'finished_at', 'updated_at'])

    total_resolved = 0
    total_created = 0
    total_skipped = 0
    total_already_resolved = 0
    total_provisional = 0
    total_reverted = 0
    total_errors = 0
    error_details: list[dict] = []
    all_code_details: list[dict] = []

    MAX_CODE_DETAILS = 500

    try:
        for i, pid in enumerate(person_ids):
            person = None
            try:
                person = Person.objects.get(person_id=pid)
                result = resolve_person_source_codes(person, derive=not refresh_record)
                total_resolved += result['resolved']
                total_created += result.get('created', 0)
                total_provisional += result.get('provisional', 0)
                total_reverted += result.get('reverted', 0)
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
                logger.exception('Error resolving a person in run %s', run_id)

            if refresh_record and person is not None:
                try:
                    _refresh_record(person)
                except Exception as exc:  # noqa: BLE001 -- the run reports it; the rows are committed
                    total_errors += 1
                    # The class only: a derivation error's text can quote a value.
                    error_details.append({'person_id': pid, 'step': RECORD_STEP,
                                          'error': type(exc).__name__})
                    logger.exception('Could not refresh the record of a person in run %s', run_id)

            run.done = i + 1
            run.resolved = total_resolved
            run.created = total_created
            run.provisional = total_provisional
            run.reverted = total_reverted
            run.skipped = total_skipped
            run.already_resolved = total_already_resolved
            run.errors = total_errors
            run.save(update_fields=[
                'done', 'resolved', 'created', 'provisional', 'reverted', 'skipped',
                'already_resolved', 'errors', 'updated_at',
            ])

        run.state = SourceCodeResolveRun.COMPLETED
    except BaseException:
        run.state = SourceCodeResolveRun.FAILED
        logger.exception('Bulk resolve run %s crashed', run_id)
        raise
    finally:
        run.error_detail = error_details
        run.code_details = all_code_details
        run.finished_at = timezone.now()
        run.save(update_fields=['state', 'error_detail', 'code_details', 'finished_at', 'updated_at'])


def create_and_dispatch_resolve_run(person_ids: list[int], user=None,
                                    refresh_record: bool = False) -> SourceCodeResolveRun:
    """Create a SourceCodeResolveRun and dispatch it (Celery or inline).

    *refresh_record*: derive each patient's record in the run (see the module
    docstring). Sent to the task only when set, so a worker still on the
    previous release during a deploy keeps taking the Patients list's runs.
    """
    run = SourceCodeResolveRun.objects.create(
        total=len(person_ids),
        created_by=user,
    )

    if getattr(settings, 'CELERY_BROKER_URL', ''):
        from omop_core.tasks import bulk_resolve_source_codes_task
        kwargs = {'refresh_record': True} if refresh_record else {}
        transaction.on_commit(
            lambda: bulk_resolve_source_codes_task.apply_async(
                args=[str(run.id), person_ids], kwargs=kwargs)
        )
    else:
        # Inline: run synchronously before returning.
        run_bulk_resolve(run.id, person_ids, refresh_record=refresh_record)

    return run
