"""Celery tasks for omop_core."""

from typing import Any

from celery import shared_task

from omop_core.models import Person


@shared_task(name='omop_core.precompute_suggest_embeddings')
def precompute_suggest_embeddings_task():
    from omop_core.services.embedding_jobs import run_suggest_embeddings

    run_suggest_embeddings()


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


@shared_task(name='omop_core.suggest_mappings')
def suggest_mappings_task(run_id: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one queued Code Mapping Suggest job.

    Failures are recorded on the SuggestRun row rather than raised: the page
    polls that row, so an exception that only reached the worker log would leave
    the curator watching a progress bar that never moves.
    """
    from omop_core.services.suggest_jobs import execute_run
    from omop_core.models import SuggestRun

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
