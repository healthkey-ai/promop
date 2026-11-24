"""A professional confirms the provisional values a patient's record shows (#2870).

The record read lays a proposed value over a field the approved record leaves
empty and marks it (``provisional_fields``; ``provisional_record.overlay``). A
patient makes one real by entering it themselves, and rejects one by clearing it
(#2671). Staff, an org admin or a doctor who reads the same record can do the
first in one step: this module turns the proposal the server shows into the
value they would have typed, and saves it through the editor's own write path
(``views.apply_patient_record_patch``), so the result is exactly an approved
edit: the value on ``patient_record``, tracked in ``user_edited_fields``,
projected into OMOP through the field's descriptor, the mark dropped, the
record-revision history naming who did it.

What it never does: approve or change a code mapping, write or delete a
clinical row of its own, touch another person, set ``validated`` (the record
attestation stays the patient's act), or read a value from the client. The
value is the server's current proposal, taken under the record's lock; a client
that names what it saw (``expected``) is refused for a field that has since
changed.

Two things decide what a field is: ``classify`` (is it confirmable at all, and
if not why) and ``confirm`` (the one write). The record read uses ``classify``
alone, so the page offers the control only where the server would act.
"""
from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from omop_core.derived_fields import PROVISIONAL_JSON_FIELDS
from omop_core.models import PatientRecord, ProposalRejection
from omop_core.services.provisional_record import (
    COMPUTED_INPUTS, REJECTION_FOLLOWERS, _PRECISION, _date_of, overlay,
)

logger = logging.getLogger(__name__)

# Why a field is not confirmed. Stable codes: a client may branch on them.
LIST_FIELD = 'list_field'            # proposed items are not the user's to write
CALCULATED = 'calculated'            # worked out from other fields; clears with them
MIRRORED = 'mirrored'                # an alias of another field; clears with it
UNIT = 'unit'                        # goes with the value it measures
FOLLOWS_FIELD = 'follows_field'      # a date's precision: confirmed with its date
ATTESTATION = 'attestation'          # validated and friends: the patient's own act
NO_WRITE_PATH = 'no_write_path'      # the editor cannot write it either
NOT_PROVISIONAL = 'not_provisional'  # the record shows no proposal there
ALREADY_APPROVED = 'already_approved'  # the record holds a value now
REJECTED = 'rejected'                # a rejection (#2671) holds; nothing is shown
CHANGED = 'changed'                  # the proposal is not the one the client saw
NOT_SEEN = 'not_seen'                # confirm-all: a proposal the client did not list
INVALID = 'invalid'                  # the value does not pass the field's own validation
NOT_SAVED = 'not_saved'              # the write path left the field empty
DISABLED = 'disabled'                # PROVISIONAL_DATA_ENABLED is off

# Not scalar proposals a person approves by writing a value.
STAFF_ONLY = frozenset({'validated', 'validated_by', 'validation_date'})

MAX_AUDITED_FIELDS = 25   # the audit note is a small JSON object (middleware._AUDIT_DETAIL_MAX_LENGTH)


def may_confirm(actor, person_id) -> bool:
    """Whether *actor* may confirm provisional values on this person's record.

    Whoever may write the patient *as a professional*: staff, an org admin (a
    grant, or the trust-derived admin ``can_write_patient`` already counts), or
    a doctor. Not an analyst (no patient access at all), and not the patient or a
    representative: they confirm by entering a value and attest the record
    themselves, so they get no second route, and an account that is both a doctor
    and this patient is the patient here.
    """
    if actor is None or not getattr(actor, 'is_authenticated', False):
        return False
    if getattr(actor, 'is_staff', False):
        return True
    from omop_core.authorization import can_write_patient, get_actor_role
    return (can_write_patient(actor, person_id)
            and get_actor_role(actor, person_id) in ('org_admin', 'doctor'))


def _writable_in_serializer(field) -> bool:
    from patient_portal.api.serializers import PatientRecordSerializer
    serializer_field = PatientRecordSerializer().fields.get(field)
    return serializer_field is not None and not serializer_field.read_only


def classify(marks, descriptor=None) -> tuple[list[str], dict[str, str]]:
    """``(confirmable, skipped)`` for the proposals a record shows.

    *marks* is the overlay's field-to-confidence map (lists included); *descriptor*
    is the write descriptor (built when not given). ``confirmable`` are the scalar
    fields the editor can write, sorted; ``skipped`` maps every other marked field to
    the reason it is not confirmed. A field that goes with another (a unit, an alias,
    a calculation, a date's precision) is skipped as such: it clears when the field it
    follows is confirmed, and is never written on its own.
    """
    if not marks:
        return [], {}
    if descriptor is None:
        from omop_core.services.write_descriptor import build_writable_field_descriptor
        descriptor = build_writable_field_descriptor()
    from omop_core.services.write_descriptor import KIND_ALIAS, KIND_COMPUTED
    confirmable, skipped = [], {}
    for field in sorted(marks):
        entry = descriptor.get(field)
        if field in STAFF_ONLY:
            skipped[field] = ATTESTATION
        elif field in PROVISIONAL_JSON_FIELDS:
            skipped[field] = LIST_FIELD
        elif field in COMPUTED_INPUTS or (entry or {}).get('kind') == KIND_COMPUTED:
            skipped[field] = CALCULATED
        elif (entry or {}).get('kind') == KIND_ALIAS:
            skipped[field] = MIRRORED
        elif field.endswith('_units'):
            skipped[field] = UNIT
        elif _date_of(field) is not None and _date_of(field) in marks:
            skipped[field] = FOLLOWS_FIELD
        elif entry is not None:
            if entry.get('writable') and entry.get('target', 'patient_record') == 'patient_record':
                confirmable.append(field)
            else:
                skipped[field] = NO_WRITE_PATH
        elif _writable_in_serializer(field):
            confirmable.append(field)       # unknown to the descriptor but writable, as the editor sends it
        else:
            skipped[field] = NO_WRITE_PATH
    return confirmable, skipped


def read_block(actor, record, marks) -> dict:
    """What the record read tells a professional: ``{confirmable, skipped}``, or ``{}``.

    Empty for anyone who may not confirm and when the record shows no proposal, so a
    patient, an analyst and a read of an approved record carry nothing extra.
    """
    if not marks or not may_confirm(actor, record.person_id):
        return {}
    confirmable, skipped = classify(marks)
    return {'provisional_confirm': {'confirmable': confirmable, 'skipped': skipped}}


def _same(shown, expected) -> bool:
    """Whether the value the client says it saw is the one the server shows now."""
    if isinstance(shown, bool) or isinstance(expected, bool):
        return shown is expected
    if shown is None or expected is None:
        return shown is None and expected is None
    try:
        return Decimal(str(shown)) == Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return str(shown) == str(expected)


def _followers(field, confirmable, not_confirmable) -> list[str]:
    """The proposed fields that are one answer with *field*: a disease's group, a date's precision.

    A precision is skipped on its own (``FOLLOWS_FIELD``) and written with its date;
    a unit or an alias is never written, it clears with the value.
    """
    return [name for name in REJECTION_FOLLOWERS.get(field, ())
            if name in confirmable or not_confirmable.get(name) == FOLLOWS_FIELD]


def audit_note(confirmed) -> dict:
    return {'provisional_confirm': {'count': len(confirmed), 'fields': sorted(confirmed)[:MAX_AUDITED_FIELDS]}}


def confirm(person, record, actor, fields=None, *, all_fields=False, expected=None) -> dict:
    """Confirm proposals on *person*'s record as *actor*, in one transaction.

    ``fields`` names the fields to confirm, ``all_fields`` takes every confirmable
    one. ``expected`` maps a field to the value the client saw: a field whose
    current proposal differs is skipped as ``changed``, and with ``all_fields`` a
    confirmable proposal the map does not list is skipped as ``not_seen`` (the
    client asked to confirm what it showed, not what arrived since). The caller has
    decided that *actor* may confirm (``may_confirm``).

    The record is locked first, as derivation and the attestation do, so a sweep
    cannot change a proposal between the check and the save. Values come from the
    overlay read under that lock, through the serializer the editor reads with, and
    are saved by ``apply_patient_record_patch`` in one call: one write, one
    recalculation, one mirror into the provisional pair. A field the record's own
    validation refuses is skipped as ``invalid``; anything unexpected rolls the
    whole request back.

    Returns ``{'confirmed': [...], 'skipped': [{'field', 'reason', 'detail'?}],
    'remaining': n}``.
    """
    fields = list(dict.fromkeys(fields or []))
    expected = expected if isinstance(expected, dict) else None
    skipped: list[dict] = []

    def skip(field, reason, detail=None):
        skipped.append({'field': field, 'reason': reason, **({'detail': detail} if detail else {})})

    if not settings.PROVISIONAL_DATA_ENABLED:
        for field in fields:
            skip(field, DISABLED)
        return {'confirmed': [], 'skipped': skipped, 'remaining': 0}

    from patient_portal.api.serializers import PatientRecordSerializer
    from patient_portal.api.views import apply_patient_record_patch

    with transaction.atomic():
        locked = PatientRecord.objects.select_for_update().get(pk=record.pk)
        shown, marks = overlay(locked)
        confirmable, not_confirmable = classify(marks)
        rejected = set(ProposalRejection.objects.filter(person=person, field_name__in=fields)
                       .values_list('field_name', flat=True)) if fields else set()

        # Which fields were asked for, and what each one is.
        if all_fields:
            wanted = list(confirmable)
            for field, reason in not_confirmable.items():
                skip(field, reason)
        else:
            wanted = []
            for field in fields:
                if field in confirmable:
                    wanted.append(field)
                elif field in marks:
                    skip(field, not_confirmable[field])
                elif field in rejected:
                    skip(field, REJECTED)
                elif _holds_value(locked, field):
                    skip(field, ALREADY_APPROVED)
                else:
                    skip(field, NOT_PROVISIONAL)

        data = PatientRecordSerializer(shown).data if wanted else {}
        chosen = []
        for field in wanted:
            if expected is not None and field in expected and not _same(data.get(field), expected[field]):
                skip(field, CHANGED)
            elif all_fields and expected is not None and field not in expected:
                skip(field, NOT_SEEN)
            else:
                chosen.append(field)
        # A field goes with its group (a disease's, a date's precision): one answer.
        for field in list(chosen):
            for follower in _followers(field, confirmable, not_confirmable):
                if follower not in chosen:
                    if expected is not None and follower in expected and not _same(data.get(follower), expected[follower]):
                        skip(follower, CHANGED)
                        continue
                    chosen.append(follower)

        patch, refused = _validated(locked, {field: data.get(field) for field in chosen})
        for field, detail in refused.items():
            skip(field, INVALID, detail)
        confirmed = []
        if patch:
            apply_patient_record_patch(person, locked, patch, actor=actor)
            locked.refresh_from_db()
            for field in patch:
                if _holds_value(locked, field):
                    confirmed.append(field)
                else:
                    skip(field, NOT_SAVED)
        remaining = len(overlay(locked)[1])
    skipped = [item for item in skipped if item['field'] not in confirmed]   # a precision went with its date
    return {'confirmed': sorted(confirmed), 'skipped': skipped, 'remaining': remaining}


def _holds_value(record, field) -> bool:
    """Whether *field*, a column of the record, holds an approved value (a name that is no column does not)."""
    from omop_core.services.patient_record_service import _is_empty
    columns = {column.name for column in PatientRecord._meta.concrete_fields}
    return field in columns and not _is_empty(getattr(record, field, None))


def _validated(record, patch):
    """``(patch, refused)``: *patch* without the fields the record's validation refuses.

    One serializer run over the whole patch finds them (the errors name their
    fields), so the number of queries does not grow with the number of fields.
    Whatever is left is saved by the caller; a failure there is not caught.
    """
    from patient_portal.api.serializers import PatientRecordSerializer
    patch, refused = dict(patch), {}
    while patch:
        serializer = PatientRecordSerializer(record, data=patch, partial=True)
        if serializer.is_valid():
            break
        bad = [field for field in serializer.errors if field in patch]
        if not bad:
            break    # an error that names no field of the patch: the save raises it
        for field in bad:
            errors = serializer.errors[field]
            refused[field] = ' '.join(str(error) for error in (errors if isinstance(errors, list) else [errors]))
            del patch[field]
    return patch, refused
