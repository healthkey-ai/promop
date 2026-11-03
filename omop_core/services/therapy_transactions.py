"""Shared transaction boundary for patient authoring and ARTEMIS materialization.

Callbacks must re-read state on every attempt and have no external side effects.
Locks acquired by an attempt are released before backoff. Callers that already
hold locks outside this boundary remain responsible for their outer transaction.
"""
from time import sleep

from django.db import OperationalError, transaction


class TherapyLineWriteConflict(ValueError):
    """The complete write rolled back; the caller may retry its intent."""


def run_therapy_write(operation, *, backoff=sleep):
    for attempt in range(3):
        try:
            with transaction.atomic():
                return operation()
        except OperationalError as exc:
            cause = exc.__cause__
            sqlstate = getattr(cause, 'sqlstate', None) or getattr(cause, 'pgcode', None)
            if sqlstate not in {'55P03', '40P01'}:
                raise
            if attempt == 2:
                raise TherapyLineWriteConflict(
                    'The treatment history is being edited concurrently. No changes were saved; retry the request.'
                ) from exc
            backoff(0.02 * (attempt + 1))


def lock_therapy_patients(person_ids):
    """One deterministic root order for single- and multi-patient documents."""
    from omop_core.models import Observation, PatientRecord, Person
    from omop_oncology.models import Episode

    ids = sorted(set(person_ids))
    for person_id in ids:
        record, _ = PatientRecord.objects.get_or_create(person_id=person_id)
        PatientRecord.objects.select_for_update().get(pk=record.pk)
    list(Person.objects.select_for_update(nowait=True).filter(pk__in=ids).order_by('pk'))
    # Generic bulk endpoints may hold a clinical row before refreshing the
    # projection. All materializers must fail fast on those rows, not just on
    # DrugExposure: retrying ARTEMIS cannot rescue a deadlock victim in bulk API.
    list(Episode.objects.select_for_update(nowait=True).filter(person_id__in=ids).order_by('pk'))
    list(Observation.objects.select_for_update(nowait=True).filter(
        person_id__in=ids, observation_source_value__startswith='LOT-',
    ).order_by('pk'))
