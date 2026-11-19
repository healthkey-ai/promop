"""Celery tasks of the patient portal."""
from celery import shared_task


@shared_task(name='patient_portal.apply_record_mapping')
def apply_record_mapping_task(response_id: str) -> None:
    """Write a submitted questionnaire response into its patient's record
    (patient_portal.survey_record_mapping), off the request that submitted it."""
    from patient_portal.survey_record_mapping import run_response

    run_response(response_id)
