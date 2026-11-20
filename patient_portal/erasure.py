"""Erasing a member's data: closing their account, or withdrawing the
permission their record depends on (docs/member_erasure_architecture.md).

- ``close_account``: the account and everything held about the person go,
  straight away, in one transaction.
- ``erase_record``: the member's health answers go and the account stays.
  ``record_consent`` calls it when a grant of a purpose marked
  ``erases_record_on_withdrawal`` ends, whatever the new answer.

Neither touches the consent ledger, which outlives the account by design
(docs/consent-v2.md). Each erasure writes its own entry to the audit trail,
in the same transaction, naming the person and who asked, with no content;
so it is logged whoever calls it, from a view, the shell or a command.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import PROTECT, Q

from omop_core.models import (
    Measurement, MeasurementOwnership, PatientDocument, PatientGroupMembership, PatientRecord, Person,
    VisitOccurrence,
)
from omop_core.services.patient_cleanup import delete_omop_clinical_rows
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.prolog_cleanup import delete_prolog_data_for_persons
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode, EpisodeEvent
from patient_portal.models import AuditEvent

logger = logging.getLogger(__name__)

# What withdrawing the record's permission leaves: the account, the
# permission and document history, the invitation, and the person's own row
# and location (name, year of birth, sex, ethnicity, country, postcode
# district), which sign-up wrote and membership needs. Every other table
# that hangs off the person is deleted, so a clinical table added later is
# erased too, not missed.
KEPT_ON_WITHDRAWAL = frozenset({
    'patient_portal.PatientUser',
    'patient_portal.ConsentEvent',
    'patient_portal.DocumentAcceptance',
    'patient_portal.PatientInvitation',
    'omop_core.PersonLanguageSkill',
    'omop_core.PersonMergeCandidate',
})

# The record's own fields that say who the member is with, not what they
# answered; carried over when the record is rebuilt.
_MEMBERSHIP_FIELDS = ('organization',)


def close_account(person: Person, identity=None, *, actor=None) -> None:
    """Delete the person, their record and every row about them, and
    ``identity`` (their sign-in) when given. ``actor`` is who asked: the
    member themselves, or staff."""
    person_id = person.person_id
    identity_id = identity.pk if identity is not None else None
    actor_id = _actor_id(actor)
    with transaction.atomic():
        files = _document_files(person)
        # Survey responses PROTECT the person and EpisodeEvent has a bare
        # integer key, so both go first; then the person's cascade does the rest.
        with suppress_patient_record_refresh():
            delete_omop_clinical_rows(person)
            person.delete()
        if identity is not None:
            identity.delete()
        _remove_files_on_commit(files, person_id)
        _log('account', person_id, actor_id, identity_id=identity_id)


def erase_record(person: Person, *, actor=None, purpose: str = '') -> PatientRecord:
    """Delete the member's health answers and keep their account.

    Their questionnaire responses, every clinical row, journal entry and
    document, and the record itself, which is then rebuilt from what is
    left: their membership and the sign-up facts. Returns the new record.
    ``purpose`` is the permission whose withdrawal asked for it.
    """
    with transaction.atomic():
        files = _document_files(person)
        # One derivation at the end, not one per deleted row (AGENTS.md, Bulk OMOP Row Deletes).
        with suppress_patient_record_refresh():
            old = PatientRecord.objects.filter(person=person).first()
            kept = {name: getattr(old, name) for name in _MEMBERSHIP_FIELDS} if old else {}

            delete_prolog_data_for_persons([person.person_id])
            # Rows that name the person or their rows by bare id, not by key.
            episode_ids = list(Episode.objects.filter(person=person).values_list('episode_id', flat=True))
            if episode_ids:
                EpisodeEvent.objects.filter(episode_id__in=episode_ids).delete()
            visit_ids = list(VisitOccurrence.objects.filter(person=person).values_list('visit_occurrence_id', flat=True))
            measurement_ids = list(Measurement.objects.filter(person=person).values_list('measurement_id', flat=True))
            if visit_ids or measurement_ids:
                MeasurementOwnership.objects.filter(
                    Q(visit_occurrence_id__in=visit_ids) | Q(measurement_id__in=measurement_ids)
                ).delete()
            # A group assigned by rule was chosen from the health answers; one
            # assigned by hand, like a personal representative, is about access.
            PatientGroupMembership.objects.filter(person_id=person.person_id, source='rule').delete()
            for relation in Person._meta.related_objects:
                model = relation.related_model
                # A many-to-many filter would select the other side's rows
                # (a whole group, say), not the person's link to them.
                if (model._meta.label in KEPT_ON_WITHDRAWAL or relation.on_delete is PROTECT
                        or relation.many_to_many):
                    continue
                model._base_manager.filter(**{relation.field.name: person}).delete()

            PatientRecord.objects.create(person=person, **kept)
        record = refresh_patient_record(person)
        _remove_files_on_commit(files, person.person_id)
        _log('record', person.person_id, _actor_id(actor), purpose=purpose)
    return record


def _actor_id(actor) -> str:
    return str(actor.pk) if getattr(actor, 'pk', None) is not None else 'system'


def _log(kind: str, person_id: int, actor_id: str, **extra) -> None:
    """The deletion log: an audit-trail row naming who was erased and who
    asked, and nothing of what was held. Written last, so the chain's lock
    is held only until the caller's transaction commits."""
    AuditEvent.objects.create(
        event_type=AuditEvent.EVENT_DELETE, method='ERASE', path=f'patient_portal.erasure:{kind}',
        user_id=actor_id, resource_id=str(person_id),
        detail={'erasure': kind, 'person_id': person_id, **{k: v for k, v in extra.items() if v}},
    )


def _document_files(person: Person) -> list:
    """Uploaded documents' stored files: deleting a row leaves its file."""
    return [(doc.file.storage, doc.file.name)
            for doc in PatientDocument.objects.filter(person=person).exclude(file='').exclude(file__isnull=True)]


def _remove_files_on_commit(files: list, person_id: int) -> None:
    """Delete the stored files once the rows are gone for good; a rolled-back
    erasure keeps them."""
    def remove():
        for storage, name in files:
            try:
                storage.delete(name)
            except Exception:  # noqa: BLE001 — one stuck file must not hide the others
                logger.warning('erasure: a document file of person %s could not be deleted', person_id, exc_info=True)

    if files:
        transaction.on_commit(remove)
