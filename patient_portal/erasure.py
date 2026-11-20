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
from typing import TYPE_CHECKING

from django.core.files.storage import Storage
from django.db import transaction
from django.db.models import PROTECT

from omop_core.models import PatientDocument, PatientRecord, Person
from omop_core.services.patient_cleanup import (
    delete_omop_clinical_rows, delete_provenance, delete_unkeyed_rows,
)
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import AuditEvent

if TYPE_CHECKING:
    from patient_portal.models import Identity

logger = logging.getLogger(__name__)

# An uploaded document's stored file: where it is kept, and its name there.
StoredFile = tuple[Storage, str]

# What withdrawing the record's permission leaves: the account, the
# permission and document history (current and legacy), the invitation, and
# the person's own row (name, year of birth) and its location's country, which
# sign-up wrote and giving the permission again needs; ``_scrub_person`` takes
# the rest of it (sex, race, ethnicity, birth day and month, address) off.
# Every other table that hangs off the person, or off one of these, is
# deleted, so a clinical table added later is erased too, not missed
# (tests/test_member_erasure.py pins the list, so a new one is a decision).
KEPT_ON_WITHDRAWAL = frozenset({
    'patient_portal.PatientUser',
    'patient_portal.PatientConsent',
    'patient_portal.ConsentEvent',
    'patient_portal.DocumentAcceptance',
    'patient_portal.PatientInvitation',
    'omop_core.PersonLanguageSkill',
    'omop_core.PersonMergeCandidate',
})

# The record's own fields that say who the member is with, not what they
# answered; carried over when the record is rebuilt.
_MEMBERSHIP_FIELDS = ('organization',)


def close_account(person: Person, identity: Identity | None = None, *, actor: Identity | None = None) -> None:
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


def erase_record(person: Person, *, actor: Identity | None = None, purpose: str = '') -> PatientRecord:
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

            tables = withdrawal_tables()
            delete_provenance(person, tables)
            delete_unkeyed_rows(person)
            # Representatives and group memberships (rule or manual) go with
            # delete_unkeyed_rows above: access to a record that is gone.
            for model, lookup in tables:
                model._base_manager.filter(**{lookup: person}).delete()

            _scrub_person(person)
            PatientRecord.objects.create(person=person, **kept)
        record = refresh_patient_record(person)
        _remove_files_on_commit(files, person.person_id)
        _log('record', person.person_id, _actor_id(actor), purpose=purpose)
    return record


def _scrub_person(person: Person) -> None:
    """Take what the permission covered off the person's own row: sex, race
    and ethnicity (special-category data) and all of the birth date but the
    year, which age checks on giving a permission again need. Name, country
    and the account's other sign-up facts stay. The location keeps its
    country (it gates which permissions are offered) and loses the rest, when
    no one else shares it."""
    from omop_core.models import Location

    for name in ('gender', 'race', 'ethnicity'):
        setattr(person, f'{name}_concept', None)
        setattr(person, f'{name}_source_value', None)
        setattr(person, f'{name}_source_concept', None)
    person.month_of_birth = person.day_of_birth = person.birth_datetime = None
    person.save()
    if person.location_id and not Person.objects.filter(location_id=person.location_id).exclude(pk=person.pk).exists():
        Location.objects.filter(pk=person.location_id).update(
            address_1=None, address_2=None, city=None, state=None, zip=None, county=None,
            location_source_value=None, latitude=None, longitude=None)


def withdrawal_tables() -> list[tuple[type, str]]:
    """``(model, lookup)`` for every table ``erase_record`` empties for a
    person: those that name the person, and those under a kept table (a
    member's messages hang off their account), except what is kept. A
    PROTECTing table is cleared first by ``delete_unkeyed_rows``; a
    many-to-many filter would select the other side's rows, not the link."""
    tables = []
    for relation in Person._meta.related_objects:
        model = relation.related_model
        if relation.many_to_many or relation.on_delete is PROTECT:
            continue
        if model._meta.label not in KEPT_ON_WITHDRAWAL:
            tables.append((model, relation.field.name))
            continue
        for below in model._meta.related_objects:
            if below.many_to_many or below.related_model._meta.label in KEPT_ON_WITHDRAWAL:
                continue
            tables.append((below.related_model, f'{below.field.name}__{relation.field.name}'))
    return tables


def _actor_id(actor: Identity | None) -> str:
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


def _document_files(person: Person) -> list[StoredFile]:
    """Uploaded documents' stored files: deleting a row leaves its file."""
    return [(doc.file.storage, doc.file.name)
            for doc in PatientDocument.objects.filter(person=person).exclude(file='').exclude(file__isnull=True)]


def _remove_files_on_commit(files: list[StoredFile], person_id: int) -> None:
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
