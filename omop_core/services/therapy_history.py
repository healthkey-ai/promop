"""The patient's confirmation that their treatment history is complete (#1924).

Recorded lines are a lower bound until someone says the list is complete. This is
a statement about the list, not clinical data: it lives beside the record, is
withdrawn by deleting it, and only then does ``PatientRecord.therapy_history_complete``
become true.
"""
from django.utils import timezone

from omop_core.models import Person, TherapyHistoryAttestation


def confirm_therapy_history(person, *, source='patient'):
    """Record that the listed lines are the patient's full history."""
    if person is None:
        raise ValueError('A therapy-history confirmation needs a patient.')
    attestation, _ = TherapyHistoryAttestation.objects.update_or_create(
        person=person,
        defaults={'confirmed_at': timezone.now(), 'source': source or 'patient'},
    )
    _refresh(person)
    return attestation


def withdraw_therapy_history(person):
    """Withdraw the confirmation; the count becomes a lower bound again."""
    if person is None:
        raise ValueError('A therapy-history confirmation needs a patient.')
    TherapyHistoryAttestation.objects.filter(person=person).delete()
    _refresh(person)


def is_therapy_history_complete(person):
    return TherapyHistoryAttestation.objects.filter(person=person).exists()


def _refresh(person):
    from omop_core.services.patient_record_service import refresh_patient_record
    refresh_patient_record(person)
