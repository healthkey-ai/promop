"""Celery tasks for omop_core."""

from typing import Any

import requests
from celery import shared_task
from celery.signals import worker_ready
from django.db import OperationalError, ProgrammingError

from omop_core.models import Person
from omop_core.services import axis_variant_jobs as _axis_variant_jobs


@worker_ready.connect
def dispatch_hospital_code_imports_on_worker_ready(**_kwargs):
    """Publish durable migration-created jobs from a worker that knows the tasks.

    Hospital-code imports and axis variant refreshes (#2021) are written by
    migrations as database rows. An older worker would discard their task
    messages as unregistered during a rolling deployment, so the worker that
    registers the tasks is the one that publishes them.
    """
    import logging
    import threading

    from omop_core.services.axis_variant_jobs import dispatch_queued_refreshes
    from omop_core.services.hospital_code_seed import dispatch_queued_imports

    log = logging.getLogger(__name__)

    def dispatch(first=False):
        # The boot pass republishes refresh requests an older worker may have
        # dropped; the rechecks only publish what nothing has published yet.
        for label, publish in (('hospital-code import', dispatch_queued_imports),
                               ('axis variant refresh', lambda: dispatch_queued_refreshes(republish=first))):
            try:
                publish()
            except (OperationalError, ProgrammingError):
                log.exception('%s dispatch deferred until the database is ready.', label)
            except Exception:
                # Never make the worker unavailable because the broker job cannot
                # be published. The queued database row remains retryable.
                log.exception('Could not dispatch %s.', label)

    dispatch(first=True)
    # Render can bring the worker up while the web service is still running its
    # release migration. Recheck without blocking worker readiness; once a task
    # id is recorded these passes are no-ops.
    for delay in (60, 180):
        timer = threading.Timer(delay, dispatch)
        timer.daemon = True
        timer.start()


@shared_task(name='omop_core.build_concept_embeddings')
def build_concept_embeddings_task():
    from omop_core.services.embedding_jobs import run_build_concept_embeddings

    run_build_concept_embeddings()


@shared_task(name='omop_core.refresh_patient_record')
def refresh_patient_record_task(person_id: int) -> dict[str, Any]:
    """Re-derive one person's PatientRecord.

    Failures are left to propagate: Celery records them as FAILURE, which is
    what the status endpoint reports. Swallowing one would leave the caller
    polling a task that says SUCCESS over a stale record.
    """
    # Lazy, the service module imports back into omop_core at load time.
    from omop_core.services.patient_record_service import refresh_patient_record

    person = Person.objects.get(person_id=person_id)
    record = refresh_patient_record(person)
    derived_at = getattr(record, 'derived_at', None)
    return {
        'person_id': person_id,
        'derived_at': derived_at.isoformat() if derived_at else None,
        'derivation_version': getattr(record, 'derivation_version', None),
    }


@shared_task(name='omop_core.project_field_to_omop')
def project_field_to_omop_task(mapping_pk: int) -> dict[str, Any]:
    """Project PatientRecord values into OMOP for one approved field mapping.

    Dispatched by the FieldConceptMapping post_save signal when a Celery
    broker is configured. Commonly populated fields (e.g. disease) touch
    every PatientRecord, so running inline would time out the curator's
    request.
    """
    from omop_core.signals import _project_field_sync

    _project_field_sync(mapping_pk)
    return {'mapping_pk': mapping_pk}


@shared_task(name='omop_core.repoint_mapping_rows', autoretry_for=(Exception,),
             retry_backoff=60, max_retries=3)
def repoint_mapping_rows_task(mapping_id: int) -> dict[str, Any]:
    """Move stored clinical rows onto an approved mapping's destination.

    Dispatched by a mapping save, which returns as soon as the mapping is
    written. Idempotent: the work lives on the mapping row, so a duplicate run
    finds nothing left to move. A failure is retried; past that, the
    destinations stay queued for ``manage.py run_queued_repoints``.
    """
    from omop_core.services.repoint_jobs import run_repoint

    return {'mapping_id': mapping_id, **run_repoint(mapping_id)}


@shared_task(name='omop_core.bulk_resolve_source_codes')
def bulk_resolve_source_codes_task(run_id: str, person_ids: list[int] | None = None) -> dict[str, Any]:
    """Run a bulk source-code resolution across multiple patients."""
    from omop_core.services.resolve_jobs import run_bulk_resolve

    run_bulk_resolve(run_id, person_ids)
    from omop_core.models import SourceCodeResolveRun
    run = SourceCodeResolveRun.objects.filter(pk=run_id).first()
    if run is None:
        return {'run_id': run_id, 'state': 'missing'}
    return {
        'run_id': run_id,
        'state': run.state,
        'done': run.done,
        'total': run.total,
        'resolved': run.resolved,
    }


@shared_task(bind=True, name='omop_core.suggest_mappings')
def suggest_mappings_task(self, run_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one queued Code Mapping Suggest job.

    Failures are recorded on the SuggestRun row rather than raised: the page
    polls that row, so an exception that only reached the worker log would leave
    the curator watching a progress bar that never moves.
    """
    from omop_core.services.suggest_jobs import execute_run
    from omop_core.models import SuggestRun

    if params.get('direction') == 'reverse' and (self.request.delivery_info or {}).get('redelivered'):
        # A worker lost after claiming the run cannot clear RUNNING itself.
        # Preserve its preview and offer a fresh search instead of acknowledging
        # the redelivery while leaving the curator polling forever.
        from django.utils import timezone
        SuggestRun.objects.filter(pk=run_id, direction='reverse', state=SuggestRun.RUNNING).update(
            state=SuggestRun.FAILURE, finished_at=timezone.now(),
            error='Source-code search was interrupted. Partial candidates remain available; retry the search.',
        )

    execute_run(run_id, params)
    run = SuggestRun.objects.filter(pk=run_id).first()
    if run is None:
        return {'run_id': run_id, 'state': 'missing'}
    return {
        'run_id': run_id,
        'state': run.state,
        'done': run.done,
        'total': run.total,
        'destinations': run.destinations,
    }


@shared_task(
    name='omop_core.sync_loinc_release',
    # Retry only what a retry can fix. A missing credential or a malformed
    # archive is permanent, and retrying it three times over an hour just
    # delays the error reaching a log. Network faults are worth another go.
    autoretry_for=(requests.RequestException,),
    # Total retry span stays well inside CELERY_TASK_TIME_LIMIT (900s default)
    # per attempt; a hard TimeLimitExceeded is not caught by autoretry_for and
    # would land mid-load, which the single transaction in sync_release()
    # rolls back rather than leaving half-applied.
    retry_backoff=60, retry_backoff_max=300, retry_jitter=True, max_retries=3,
)
def sync_loinc_release_task():
    from omop_core.services.loinc_release import sync_release

    sync_release()


@shared_task(
    bind=True,
    name='omop_core.sync_athena_vocabulary',
    # A full first load scans the governed Athena export and publishes table
    # checksums. Render pre-deploy cannot accommodate that work; the dedicated
    # worker task can, and its transaction makes worker-loss redelivery safe.
    time_limit=6 * 60 * 60,
)
def sync_athena_vocabulary_task(self, sync_id: int) -> dict[str, Any]:
    from django.core.management import call_command
    from django.utils import timezone

    from omop_core.models import AthenaVocabularySync

    sync = AthenaVocabularySync.objects.get(pk=sync_id)
    if sync.outcome in ('current', 'applied'):
        return {'sync_id': sync_id, 'outcome': sync.outcome}

    AthenaVocabularySync.objects.filter(pk=sync_id).update(
        outcome='running', started_at=timezone.now(), completed_at=None,
        failure_reason='', task_id=self.request.id or sync.task_id,
    )
    try:
        call_command(
            'sync_athena_vocabulary', gdrive=sync.source_url, apply=True,
            sync_id=sync_id,
        )
    except Exception as exc:
        # The command records failures reached inside its handler. Keep the
        # durable state correct for failures before that catch as well (option
        # parsing, command discovery, or a future refactor).
        AthenaVocabularySync.objects.filter(pk=sync_id, outcome='running').update(
            outcome='failed', failure_reason=str(exc), completed_at=timezone.now(),
        )
        raise
    sync.refresh_from_db()
    return {'sync_id': sync_id, 'outcome': sync.outcome}


@shared_task(
    bind=True,
    name='omop_core.import_hospital_code_seed',
    time_limit=6 * 60 * 60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def import_hospital_code_seed_task(self, import_id: int) -> dict[str, Any]:
    """Download and install one checksum-pinned hospital-code seed.

    A Render deploy can replace a worker while this multi-minute import is in
    progress.  Acknowledge only after completion and ask the broker to
    redeliver on worker loss; ``execute_import`` is deliberately idempotent.
    """
    from omop_core.models import HospitalCodeImport
    from omop_core.services.hospital_code_seed import (
        HospitalCodeDownloadError,
        execute_import,
    )

    try:
        return execute_import(import_id)
    except HospitalCodeDownloadError as exc:
        if self.request.retries >= 3:
            raise
        HospitalCodeImport.objects.filter(pk=import_id).update(
            outcome='queued', failure_reason=str(exc), completed_at=None,
        )
        raise self.retry(exc=exc, countdown=min(60 * (2 ** self.request.retries), 300))


@shared_task(
    bind=True,
    name='omop_core.refresh_axis_variants',
    # One request walks every multi-unit code on the instance. HealthTree's did
    # not finish inside the Cloud Run migrate job's hour (#2021); give it the
    # Athena sync's limit, which the Redis visibility timeout (25200s) exceeds.
    # The soft limit fires first, inside run_refresh's try, so an overrun is
    # recorded as failed instead of leaving the row running forever.
    time_limit=_axis_variant_jobs.TIME_LIMIT,
    soft_time_limit=_axis_variant_jobs.SOFT_TIME_LIMIT,
    acks_late=True,
    reject_on_worker_lost=True,
)
def refresh_axis_variants_task(self, request_id: int) -> dict[str, Any]:
    """Perform one durable axis variant refresh request.

    The request row is claimed atomically, so a redelivery or a duplicate
    publish from the worker-ready sweep runs nothing twice. A failure is
    recorded on the row and raised, so Celery reports FAILURE as well.
    """
    return _axis_variant_jobs.run_refresh(request_id)
