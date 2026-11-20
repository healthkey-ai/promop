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
    VisitOccurrence,
)
from omop_core.services.prolog_cleanup import delete_prolog_data_for_persons
from omop_oncology.models import Episode, EpisodeEvent


# Primary keys a ProvenanceRecord's integer object_id can name.
_INTEGER_KEYS = frozenset({
    'AutoField', 'BigAutoField', 'SmallAutoField', 'IntegerField', 'BigIntegerField',
    'PositiveIntegerField', 'PositiveBigIntegerField',
})


def delete_unkeyed_rows(person):
    """Delete the rows about ``person`` that no cascade from the person reaches.

    A PROlog survey response PROTECTs its participant; ``EpisodeEvent`` and
    ``MeasurementOwnership`` hold bare ids. Shared by every path that deletes a
    person's clinical rows (``delete_omop_clinical_rows``, member erasure), so
    the list is kept once. Call inside an atomic transaction.
    """
    delete_prolog_data_for_persons([person.person_id])

    # Rows that name the person by a bare id (no foreign key, so no cascade):
    # who may act for them, and the groups they were placed in.
    from omop_core.models import PatientGroupMembership, PersonalRepresentative
    PersonalRepresentative.objects.filter(person_id=person.person_id).delete()
    PatientGroupMembership.objects.filter(person_id=person.person_id).delete()

    episode_ids = list(Episode.objects.filter(person=person).values_list('episode_id', flat=True))
    if episode_ids:
        EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()

    visit_ids = list(VisitOccurrence.objects.filter(person=person).values_list('visit_occurrence_id', flat=True))
    measurement_ids = list(Measurement.objects.filter(person=person).values_list('measurement_id', flat=True))
    if visit_ids or measurement_ids:
        MeasurementOwnership.objects.filter(
            Q(visit_occurrence_id__in=visit_ids) | Q(measurement_id__in=measurement_ids)
        ).delete()


def delete_provenance(person, tables, *, about_the_person=False):
    """Delete the ``ProvenanceRecord`` rows of the rows about to be deleted.

    Provenance points at its row through a generic key, which no cascade
    follows. ``tables`` is ``(model, lookup)`` pairs, the lookup selecting the
    person's rows in that model. ``about_the_person`` also takes every row
    naming the person as its target and the person's own row: for when the
    person goes too. Call before the rows themselves are deleted.
    """
    from django.contrib.contenttypes.models import ContentType
    from omop_core.models import Person, ProvenanceRecord

    q = Q()
    if about_the_person:
        q |= Q(target_patient_id=str(person.person_id))
        q |= Q(content_type=ContentType.objects.get_for_model(Person), object_id=person.person_id)
    for model, lookup in tables:
        if model._meta.pk.get_internal_type() not in _INTEGER_KEYS:
            continue
        q |= Q(content_type=ContentType.objects.get_for_model(model),
               object_id__in=model._base_manager.filter(**{lookup: person}).values('pk'))
    if q:
        ProvenanceRecord.objects.filter(q).delete()


def person_tables():
    """``(model, lookup)`` for every table that names a person by key."""
    from omop_core.models import Person

    return [(r.related_model, r.field.name) for r in Person._meta.related_objects if not r.many_to_many]


def delete_omop_clinical_rows(person):
    """Delete patient-scoped rows that are not removed automatically by Person.delete().

    Call this inside an atomic transaction before deleting the Person. The caller is
    still responsible for deleting the linked Identity, then the Person itself.
    """
    delete_provenance(person, person_tables(), about_the_person=True)
    delete_unkeyed_rows(person)
    Episode.objects.filter(person=person).delete()

    ConditionOccurrence.objects.filter(person=person).delete()
    DrugExposure.objects.filter(person=person).delete()
    Measurement.objects.filter(person=person).delete()
    Observation.objects.filter(person=person).delete()
    ProcedureOccurrence.objects.filter(person=person).delete()
    VisitOccurrence.objects.filter(person=person).delete()
    Death.objects.filter(person=person).delete()

    PatientDocument.objects.filter(person=person).delete()
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

    PatientDocument.objects.filter(person_id__in=person_ids).delete()
    PatientTrialEnrollment.objects.filter(person_id__in=person_ids).delete()
    PatientGroupMembership.objects.filter(person_id__in=person_ids).delete()
    PersonalRepresentative.objects.filter(person_id__in=person_ids).delete()
