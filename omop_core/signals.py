"""
OMOP post_save signals — auto-refresh PatientRecord whenever OMOP tables are written.

Any write to ConditionOccurrence, DrugExposure, Measurement, Observation, or
ProcedureOccurrence triggers refresh_patient_record(person), keeping PatientRecord in
sync without requiring direct writes to the denormalized table.

Signal suppression during bulk uploads:
  Option 1 — context manager (preferred for bulk operations like upload_fhir):
      from omop_core.signals import suppress_patient_record_refresh
      with suppress_patient_record_refresh():
          # all OMOP writes here — signals fire but refresh is skipped
          ...
      refresh_patient_record(person)  # single refresh at the end

  Option 2 — per-instance flag:
      instance._skip_patient_record_refresh = True
      instance.save()
"""

import logging
import threading
from contextlib import contextmanager

from django.conf import settings
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver

from .models import (
    ConditionOccurrence, DrugExposure, FieldConceptMapping, Measurement,
    Observation, ProcedureOccurrence,
    PersonLanguageSkill,
)

logger = logging.getLogger(__name__)

# Thread-local flag — suppresses all signal-triggered refreshes for the
# current thread without affecting other concurrent requests.
_suppress = threading.local()


@contextmanager
def suppress_patient_record_refresh():
    """Suppress signal-triggered PatientRecord refreshes for the current thread.

    Use around bulk OMOP writes and call refresh_patient_record() explicitly
    once at the end to keep PatientRecord in sync with a single DB round-trip.

    Re-entrant safe: nested calls preserve the outer suppression state.
    """
    was_active = getattr(_suppress, 'active', False)
    _suppress.active = True
    try:
        yield
    finally:
        _suppress.active = was_active


# Backward-compat alias for any out-of-tree callers — remove once all callers are updated.
suppress_patient_info_refresh = suppress_patient_record_refresh


def _refresh_for_instance(instance):
    """Call refresh_patient_record for the person linked to an OMOP event instance."""
    if getattr(_suppress, 'active', False):
        return
    if getattr(instance, '_skip_patient_record_refresh', False):
        return
    try:
        person = instance.person
        # Lazy import to avoid circular-import issues at module load time
        from omop_core.services.patient_record_service import refresh_patient_record
        refresh_patient_record(person)
    except Exception as exc:
        # Signals must not raise — log and continue
        logger.warning(
            "PatientRecord refresh failed for person_id=%s after %s save: %s",
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


# ---------------------------------------------------------------------------
# Primary language
# ---------------------------------------------------------------------------

@receiver(post_delete, sender=PersonLanguageSkill)
def person_language_skill_deleted(sender, instance, **kwargs):
    """Promote another row when the primary language row is deleted.

    PersonLanguageSkill.save() makes the first row a person gets their primary
    one, but that only fires on insert. Since a person can hold several rows per
    language, deleting the flagged row would otherwise leave them with languages
    and no primary, and nothing would ever restore one -- the database enforces
    at most one primary, not at least one.

    Promotes the earliest surviving row, so the choice is deterministic rather
    than whatever the database happens to return first.

    Deleting a Person cascades to these rows, and this receiver fires for each.
    The promotion it does then is wasted but harmless: the promoted row is
    itself deleted moments later, still inside the same cascade.
    """
    if not instance.is_primary:
        return

    survivor = (
        PersonLanguageSkill.objects
        .filter(person_id=instance.person_id)
        .order_by('created_date', 'id')
        .first()
    )
    if survivor is None:
        return

    PersonLanguageSkill.objects.filter(pk=survivor.pk).update(is_primary=True)


# ---------------------------------------------------------------------------
# OMOP projection on FieldConceptMapping approval
# ---------------------------------------------------------------------------

def _project_field_sync(mapping_pk):
    """Run projection synchronously — shared by the Celery task and inline path."""
    try:
        from omop_core.models import FieldConceptMapping as FCM
        from omop_core.services.omop_projection import project_field_to_omop
        mapping = FCM.objects.filter(pk=mapping_pk).first()
        if mapping:
            project_field_to_omop(mapping)
    except Exception:
        logger.warning('OMOP mapping projection failed')


@receiver(post_save, sender=FieldConceptMapping)
def field_concept_mapping_saved(sender, instance, **kwargs):
    """Project PatientRecord values into OMOP when a mapping is approved.

    This bridges direct user edits (stored on PatientRecord) into the OMOP
    tables, so derivation picks them up and the field drops out of
    user_edited_fields.

    When a Celery broker is configured the projection is deferred to a worker,
    because a commonly populated field (e.g. ``disease``) touches every
    PatientRecord and would time out the curator's request.
    """
    if instance.status != 'approved':
        return
    if not instance.concept_id or not instance.omop_table:
        return

    mapping_pk = instance.pk

    if getattr(settings, 'CELERY_BROKER_URL', ''):
        from django.db import transaction as txn
        txn.on_commit(lambda pk=mapping_pk: _dispatch_projection(pk))
    else:
        _project_field_sync(mapping_pk)


def _dispatch_projection(mapping_pk):
    """Send projection to Celery worker."""
    try:
        from omop_core.tasks import project_field_to_omop_task
        project_field_to_omop_task.delay(mapping_pk)
    except Exception:
        logger.warning('Failed to dispatch OMOP projection task; running inline')
        _project_field_sync(mapping_pk)
