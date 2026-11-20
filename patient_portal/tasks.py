"""Celery tasks of the patient portal."""
from celery import shared_task


@shared_task(name='patient_portal.apply_record_mapping')
def apply_record_mapping_task(response_id: str) -> None:
    """Write a submitted questionnaire response into its patient's record
    (patient_portal.survey_record_mapping), off the request that submitted it."""
    from patient_portal.survey_record_mapping import run_response

    run_response(response_id)


@shared_task(name='patient_portal.send_password_reset', bind=True, max_retries=3)
def send_password_reset_task(self, identity_id: int, by_administrator: bool) -> None:
    """Email a password-reset link, off the request that asked for it, so its
    response time does not say whether the account exists
    (patient_portal.api.password_reset.queue_password_reset). A mail provider
    that fails is tried again, three times over about seven minutes; the
    link's token stays valid for days."""
    from patient_portal.api.password_reset import PasswordResetEmailError, send_password_reset_to

    try:
        send_password_reset_to(identity_id, by_administrator=by_administrator, raise_on_failure=True)
    except PasswordResetEmailError as exc:
        raise self.retry(exc=exc, countdown=60 * 2 ** self.request.retries)
