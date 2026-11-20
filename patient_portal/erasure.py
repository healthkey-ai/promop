"""Erasing a member's data: closing their account, or withdrawing the
permission their record depends on (docs/member-erasure.md).

- ``close_account``: the account and everything held about the person go,
  straight away, in the caller's transaction.
- ``erase_record``: the member's health answers go and the account stays.
  ``record_consent`` calls it when a member withdraws a purpose marked
  ``erases_record_on_withdrawal``.

Neither touches the consent ledger, which outlives the account by design
(docs/consent-v2.md). Each erasure is recorded in the audit trail by the view
that asked for it, with the person's id and no content (``audit_detail``).
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import PROTECT, Q

from omop_core.models import (
    Measurement, MeasurementOwnership, PatientGroupMembership, PatientRecord, Person, VisitOccurrence,
)
from omop_core.services.patient_cleanup import delete_omop_clinical_rows
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.prolog_cleanup import delete_prolog_data_for_persons
from omop_core.signals import suppress_patient_record_refresh
from omop_oncology.models import Episode, EpisodeEvent

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


def close_account(person: Person, identity=None) -> None:
    """Delete the person, their record and every row about them, and
    ``identity`` (their sign-in) when given. Call inside a transaction."""
    # Survey responses PROTECT the person and EpisodeEvent has a bare
    # integer key, so both go first; then the person's cascade does the rest.
    with suppress_patient_record_refresh():
        delete_omop_clinical_rows(person)
        person.delete()
    if identity is not None:
        identity.delete()


def erase_record(person: Person) -> PatientRecord:
    """Delete the member's health answers and keep their account.

    Their questionnaire responses, every clinical row, journal entry and
    document, and the record itself, which is then rebuilt from what is
    left: their membership and the sign-up facts. Returns the new record.
    """
    # One derivation at the end, not one per deleted row (AGENTS.md, Bulk OMOP Row Deletes).
    with transaction.atomic(), suppress_patient_record_refresh():
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
            if model._meta.label in KEPT_ON_WITHDRAWAL or relation.on_delete is PROTECT:
                continue
            model._base_manager.filter(**{relation.field.name: person}).delete()

        PatientRecord.objects.create(person=person, **kept)
    return refresh_patient_record(person)
