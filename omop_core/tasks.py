"""Celery tasks for omop_core."""

from typing import Any

from celery import shared_task

from omop_core.models import Person


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
