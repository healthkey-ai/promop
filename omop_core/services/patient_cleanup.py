import logging

from django.contrib.contenttypes.models import ContentType
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Q

from omop_core.models import (
    ConditionOccurrence,
    Death,
    DrugExposure,
    Measurement,
    MeasurementOwnership,
    Observation,
    PatientDocument,
    PatientGroupMembership,
    PatientTrialEnrollment,
    PersonalRepresentative,
    ProcedureOccurrence,
    ProvenanceRecord,
    VisitOccurrence,
)
from omop_core.services.prolog_cleanup import delete_prolog_data_for_persons
from omop_oncology.models import Episode, EpisodeEvent

logger = logging.getLogger(__name__)


def delete_document_files(documents) -> None:
    """Remove the stored files behind these ``PatientDocument`` rows once the
    transaction commits.

    Django never deletes a ``FileField``'s file with its row, so a deleted
    account's uploads would stay in the media store with no row left to find
    them by. After commit, because a rolled-back deletion must keep them. A
    file the store has already lost is logged, not raised: the row is gone
    either way. A document held only by an external ``file_url`` is not
    promop's to delete.
    """
    names = [name for name in documents.exclude(file='').exclude(file__isnull=True)
             .values_list('file', flat=True) if name]
    if not names:
        return

    def remove():
        for name in names:
            try:
                default_storage.delete(name)
            except Exception:  # noqa: BLE001 - the store decides what can fail
                logger.warning('document_file_not_deleted name=%s', name, exc_info=True)

    transaction.on_commit(remove)



def delete_provenance_for(model_cls, row_ids) -> None:
    """Remove the ProvenanceRecord rows describing these rows.

    A GenericForeignKey has no database cascade, so provenance outlives the
    row it describes unless deleted by content object. ``target_patient_id``
    is a free text field (a FHIR Patient id on the sync paths), so it cannot
    be the key.
    """
    row_ids = list(row_ids)
    if row_ids:
        ProvenanceRecord.objects.filter(
            content_type=ContentType.objects.get_for_model(model_cls), object_id__in=row_ids,
        ).delete()


def _delete_with_provenance(queryset) -> None:
    """Delete the rows of ``queryset`` and the provenance describing them."""
    delete_provenance_for(queryset.model, queryset.values_list('pk', flat=True))
    queryset.delete()


def delete_omop_clinical_rows(person):
    """Delete patient-scoped rows that are not removed automatically by Person.delete().

    Call this inside an atomic transaction before deleting the Person. The caller is
    still responsible for deleting the linked Identity, then the Person itself.
    Provenance describing any of these rows goes with them.
    """
    # A PROlog survey response PROTECTs its participant, so these come out
    # before anything tries to delete the Person.
    delete_prolog_data_for_persons([person.person_id])

    episode_ids = list(Episode.objects.filter(person=person).values_list('episode_id', flat=True))
    if episode_ids:
        EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()
        delete_provenance_for(Episode, episode_ids)
        Episode.objects.filter(person=person).delete()

    visit_ids = list(VisitOccurrence.objects.filter(person=person).values_list('visit_occurrence_id', flat=True))
    measurement_ids = list(Measurement.objects.filter(person=person).values_list('measurement_id', flat=True))
    if visit_ids or measurement_ids:
        MeasurementOwnership.objects.filter(
            Q(visit_occurrence_id__in=visit_ids) | Q(measurement_id__in=measurement_ids)
        ).delete()

    delete_document_files(PatientDocument.objects.filter(person=person))
    for model in (ConditionOccurrence, DrugExposure, Measurement, Observation,
                  ProcedureOccurrence, VisitOccurrence, Death, PatientDocument):
        _delete_with_provenance(model.objects.filter(person=person))
    PatientTrialEnrollment.objects.filter(person=person).delete()
    PatientGroupMembership.objects.filter(person_id=person.person_id).delete()
    PersonalRepresentative.objects.filter(person_id=person.person_id).delete()


def delete_omop_clinical_rows_bulk(person_ids):
    """Delete patient-scoped rows for many people using set-based SQL deletes.

    This is the fast path for org teardown. It issues table-level deletes scoped
    by the cohort instead of looping person-by-person in Python.
    """
    person_ids = list(person_ids)
    if not person_ids:
        return

    delete_prolog_data_for_persons(person_ids)

    episode_ids = list(
        Episode.objects.filter(person_id__in=person_ids).values_list('episode_id', flat=True)
    )
    if episode_ids:
        EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()
        Episode.objects.filter(person_id__in=person_ids).delete()

    visit_ids = list(
        VisitOccurrence.objects.filter(person_id__in=person_ids).values_list('visit_occurrence_id', flat=True)
    )
    measurement_ids = list(
        Measurement.objects.filter(person_id__in=person_ids).values_list('measurement_id', flat=True)
    )
    if visit_ids or measurement_ids:
        MeasurementOwnership.objects.filter(
            Q(visit_occurrence_id__in=visit_ids) | Q(measurement_id__in=measurement_ids)
        ).delete()

    ConditionOccurrence.objects.filter(person_id__in=person_ids).delete()
    DrugExposure.objects.filter(person_id__in=person_ids).delete()
    Measurement.objects.filter(person_id__in=person_ids).delete()
    Observation.objects.filter(person_id__in=person_ids).delete()
    ProcedureOccurrence.objects.filter(person_id__in=person_ids).delete()
    VisitOccurrence.objects.filter(person_id__in=person_ids).delete()
    Death.objects.filter(person_id__in=person_ids).delete()

    delete_document_files(PatientDocument.objects.filter(person_id__in=person_ids))
    PatientDocument.objects.filter(person_id__in=person_ids).delete()
    PatientTrialEnrollment.objects.filter(person_id__in=person_ids).delete()
    PatientGroupMembership.objects.filter(person_id__in=person_ids).delete()
    PersonalRepresentative.objects.filter(person_id__in=person_ids).delete()
