"""
OMOP post_save signals — auto-refresh PatientInfo whenever OMOP tables are written.

Any write to ConditionOccurrence, DrugExposure, Measurement, Observation, or
ProcedureOccurrence triggers refresh_patient_info(person), keeping PatientInfo in
sync without requiring direct writes to the denormalized table.

Signal suppression during bulk uploads:
  Use bulk_create() (signals are not fired) OR set the `_skip_patient_info_refresh`
  flag on the instance before saving:
      instance._skip_patient_info_refresh = True
      instance.save()
"""

import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import (
    ConditionOccurrence, DrugExposure, Measurement,
    Observation, ProcedureOccurrence,
)

logger = logging.getLogger(__name__)


def _refresh_for_instance(instance):
    """Call refresh_patient_info for the person linked to an OMOP event instance."""
    if getattr(instance, '_skip_patient_info_refresh', False):
        return
    try:
        person = instance.person
        # Lazy import to avoid circular-import issues at module load time
        from omop_core.services.patient_info_service import refresh_patient_info
        refresh_patient_info(person)
    except Exception as exc:
        # Signals must not raise — log and continue
        logger.warning(
            "PatientInfo refresh failed for person_id=%s after %s save: %s",
            getattr(instance, 'person_id', '?'),
            type(instance).__name__,
            exc,
        )


@receiver(post_save, sender=ConditionOccurrence)
def condition_occurrence_saved(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_save, sender=DrugExposure)
def drug_exposure_saved(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_save, sender=Measurement)
def measurement_saved(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_save, sender=Observation)
def observation_saved(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_save, sender=ProcedureOccurrence)
def procedure_occurrence_saved(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_delete, sender=ConditionOccurrence)
def condition_occurrence_deleted(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_delete, sender=DrugExposure)
def drug_exposure_deleted(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_delete, sender=Measurement)
def measurement_deleted(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_delete, sender=Observation)
def observation_deleted(sender, instance, **kwargs):
    _refresh_for_instance(instance)


@receiver(post_delete, sender=ProcedureOccurrence)
def procedure_occurrence_deleted(sender, instance, **kwargs):
    _refresh_for_instance(instance)
