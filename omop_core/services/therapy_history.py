"""The patient's confirmation that their treatment history is complete (#1924).

Recorded lines are a lower bound until someone says the list is complete. This is
a statement about the list, not clinical data: it lives beside the record, is
withdrawn by deleting it, and only then does ``PatientRecord.therapy_history_complete``
become true.

Both writers derive the record once and return it, so a caller has nothing
further to refresh: derivation is the expensive part of this write.
"""
from django.utils import timezone

from omop_core.models import Person, TherapyHistoryAttestation

#: Who may be recorded as making the statement. Provenance only, never a
#: clinical decision; the API layer decides which applies, the client does not.
SOURCES = ('patient', 'clinician', 'host')


def confirm_therapy_history(person, *, source='patient'):
    """Record that the listed lines are the patient's full history.

    Returns the re-derived ``PatientRecord``.
    """
    if person is None:
        raise ValueError('A therapy-history confirmation needs a patient.')
    if source not in SOURCES:
        raise ValueError(f'Unknown therapy-history attestation source {source!r}.')
    TherapyHistoryAttestation.objects.update_or_create(
        person=person,
        defaults={'confirmed_at': timezone.now(), 'source': source},
    )
    return _refresh(person)


def withdraw_therapy_history(person):
    """Withdraw the confirmation; the count becomes a lower bound again.

    Returns the re-derived ``PatientRecord``.
    """
    if person is None:
        raise ValueError('A therapy-history confirmation needs a patient.')
    TherapyHistoryAttestation.objects.filter(person=person).delete()
    return _refresh(person)


def is_therapy_history_complete(person):
    return TherapyHistoryAttestation.objects.filter(person=person).exists()


def _refresh(person):
    from omop_core.services.patient_record_service import refresh_patient_record
    return refresh_patient_record(person)
