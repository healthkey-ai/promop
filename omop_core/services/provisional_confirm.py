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
from omop_core.models import PatientRecord, ProposalRejection, RecordRevision
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
NOT_SHOWN = 'not_shown'              # no tab shows it for this record: nobody could see it to confirm
NO_PROJECTION = 'no_projection'      # nothing would acknowledge it: it would stay pinned as an edit
DROPPED_WITH_GROUP = 'dropped_with_group'  # discarded with the group it belongs to, as a typed edit does

# Not scalar proposals a person approves by writing a value.
STAFF_ONLY = frozenset({'validated', 'validated_by', 'validation_date'})

def _own_or_represented(actor, person_id) -> bool:
    """Whether *actor* is this person (their patient account) or a verified representative of them."""
    from omop_core.models import PersonalRepresentative
    from patient_portal.models import PatientUser
    return (PatientUser.objects.filter(identity=actor, person_id=person_id).exists()
            or PersonalRepresentative.objects.filter(
                representative=actor, person_id=person_id, verification_status='VERIFIED').exists())


def may_confirm(actor, person_id) -> bool:
    """Whether *actor* may confirm provisional values on this person's record.

    Whoever may write the patient *as a professional*: staff, an org admin (a
    grant, or the trust-derived admin ``can_write_patient`` already counts), or
    a doctor. Not an analyst (no patient access at all), and not the patient or a
    representative, **staff included**: they confirm by entering a value and attest
    the record themselves, so they get no second route, and an account that is both a
    doctor (or staff) and this patient is the patient here.
    """
    if actor is None or not getattr(actor, 'is_authenticated', False):
        return False
    if _own_or_represented(actor, person_id):
        return False
    if getattr(actor, 'is_staff', False):
        return True
    # Not their own record or one they represent (checked above); then the professional path
    # of ``can_write_patient``, on its own: a doctor or org_admin grant, or an admin of the
    # patient's organization. A test holds this to ``can_write_patient``.
    from omop_core.authorization import has_professional_write
    return has_professional_write(actor, person_id)


def _has_way_to_be_acknowledged(entry) -> bool:
    """Whether a confirmed value on this field is carried by something besides ``user_edited_fields``.

    An OMOP projection (the approved fact the write adds, which derivation then reads
    back and the pin lapses), or a Person/Location column. A field with neither stays
    pinned in ``user_edited_fields`` for as long as the derived value differs: a newer
    approved fact would never replace it. A typed edit lives with that; a confirmation
    does not offer it (``no_projection``).
    """
    from omop_core.services.omop_projection import projection_for_descriptor
    return bool(projection_for_descriptor(entry)) or (entry or {}).get('projection_target') in ('person', 'location')


def classify(marks, disease=None, descriptor=None) -> tuple[list[str], dict[str, str]]:
    """``(confirmable, skipped)`` for the proposals a record shows.

    *marks* is the overlay's field-to-confidence map (lists included), *disease* the
    disease text the page shows (it picks the disease tab's section), *descriptor* the
    write descriptor (built, only when a field still needs it, when not given).
    ``confirmable`` are the scalar fields the person sees a Confirm control on and the
    editor can write and OMOP can acknowledge, sorted; ``skipped`` maps every other
    marked field to why. A field that goes with another (a unit, an alias, a calculation,
    a date's precision) clears when the field it follows is confirmed and is never
    written on its own. **Confirm all only ever covers what is on the screen**:
    ``not_shown`` is a marked field no tab renders for this record (the on-screen rule
    of ``attestation_blockers``), which a person could not see to confirm.
    """
    if not marks:
        return [], {}
    from omop_core.services.patient_visible_fields import on_screen
    shown = on_screen(disease)
    confirmable, skipped, pending = [], {}, []
    for field in sorted(marks):
        if field in STAFF_ONLY:
            skipped[field] = ATTESTATION
        elif field in PROVISIONAL_JSON_FIELDS:
            skipped[field] = LIST_FIELD
        elif field in COMPUTED_INPUTS:
            skipped[field] = CALCULATED
        elif field.endswith('_units'):
            skipped[field] = UNIT
        elif _date_of(field) is not None and _date_of(field) in marks:
            skipped[field] = FOLLOWS_FIELD
        elif field not in shown:
            skipped[field] = NOT_SHOWN
        else:
            pending.append(field)
    if pending:
        if descriptor is None:
            from omop_core.services.write_descriptor import build_writable_field_descriptor
            descriptor = build_writable_field_descriptor()
        from omop_core.services.write_descriptor import KIND_ALIAS, KIND_COMPUTED
        for field in pending:
            entry = descriptor.get(field)
            kind = (entry or {}).get('kind')
            if kind == KIND_COMPUTED:
                skipped[field] = CALCULATED
            elif kind == KIND_ALIAS:
                skipped[field] = MIRRORED
            elif entry is None or not entry.get('writable') or entry.get('target', 'patient_record') != 'patient_record':
                skipped[field] = NO_WRITE_PATH
            elif not _has_way_to_be_acknowledged(entry):
                skipped[field] = NO_PROJECTION
            else:
                confirmable.append(field)
    return confirmable, skipped


def groups(marks, confirmable, skipped) -> list[list[str]]:
    """The fields confirmed together, each a sorted list of two or more (a lone field has no group).

    A field goes with those that are one answer with it (``REJECTION_FOLLOWERS``: a
    date and its precision, a disease and its group), as the PATCH's mirror treats them.
    A member is a confirmable field, or a precision (``follows_field``) that is marked.
    Overlapping groups are merged, so deciding a group never splits another.
    """
    members = set(confirmable) | {f for f, why in skipped.items() if why == FOLLOWS_FIELD}
    parent = {f: f for f in members}

    def find(f):
        while parent[f] != f:
            parent[f] = parent[parent[f]]
            f = parent[f]
        return f
    for leader in confirmable:
        for follower in REJECTION_FOLLOWERS.get(leader, ()):
            if follower in members and follower in marks:
                parent[find(follower)] = find(leader)
    merged = {}
    for f in members:
        merged.setdefault(find(f), []).append(f)
    return sorted(sorted(group) for group in merged.values() if len(group) > 1)


def read_block(actor, record, shown, marks) -> dict:
    """What the record read tells a professional: ``{confirmable, skipped, groups}``, or ``{}``.

    *shown* is the overlaid record (its disease picks the on-screen section). Cheap
    name rules run first and the write descriptor is built only when a field is left
    that needs it; whether the reader may confirm at all is asked last, since it is the
    part that costs queries per reader. Empty for anyone who may not confirm and when
    the record shows no proposal, so a patient, an analyst and a read of an approved
    record carry nothing extra.
    """
    if not marks:
        return {}
    confirmable, skipped = classify(marks, getattr(shown, 'disease', None))
    if not may_confirm(actor, record.person_id):
        return {}
    return {'provisional_confirm': {
        'confirmable': confirmable, 'skipped': skipped, 'groups': groups(marks, confirmable, skipped)}}


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


def audit_note(mode) -> dict:
    """The audit row's note: that a confirmation was made and in which mode. No field names: the
    revisions name the fields, and the audit trail stays free of anything about the record."""
    return {'provisional_confirm': {'mode': mode}}


class _Unprojected(Exception):
    """A confirmed field is still pinned as a pending edit after the write: nothing acknowledged it."""

    def __init__(self, fields):
        super().__init__(', '.join(sorted(fields)))
        self.fields = set(fields)


def confirm(person, record, actor, fields=None, *, all_fields=False, expected=None) -> dict:
    """Confirm proposals on *person*'s record as *actor*, in one transaction.

    ``fields`` names the fields to confirm, ``all_fields`` takes every confirmable
    one. ``expected`` maps a field to the value the client saw; the caller requires it
    for ``all`` (the view answers 400 without it). The caller has decided that *actor*
    may confirm (``may_confirm``).

    Fields that are one answer (``groups``: a date and its precision) are decided
    together **before** anything is written. With ``expected`` given, every member of a
    group must be listed in it and equal the server's current proposal; otherwise the
    whole group is skipped (``changed`` when a member differs, else ``not_seen``) and
    nothing of it is written. The members come from the server's own groups, never from
    the client. Each field is reported at most once.

    The record is locked first, as derivation and the attestation do, so a sweep
    cannot change a proposal between the check and the save. Values come from the
    overlay read under that lock, through the serializer the editor reads with, and
    are saved by ``apply_patient_record_patch`` in one call: one write, one
    recalculation, one mirror into the provisional pair. The approved fact is dated
    when the reading was taken (``provisional_record.proposal_dates``), not today.
    A group the record's own validation refuses is skipped as ``invalid``; a group
    whose value nothing acknowledged after the write (still a pending edit) is taken
    back to a savepoint and skipped as ``no_projection``; anything unexpected rolls
    the whole request back.

    Returns ``{'confirmed': [...], 'skipped': [{'field', 'reason', 'detail'?}],
    'remaining': n}``.
    """
    fields = list(dict.fromkeys(fields or []))
    expected = expected if isinstance(expected, dict) else None
    skipped: dict[str, dict] = {}

    def skip(field, reason, detail=None):
        skipped.setdefault(field, {'field': field, 'reason': reason, **({'detail': detail} if detail else {})})

    if not settings.PROVISIONAL_DATA_ENABLED:
        for field in fields:
            skip(field, DISABLED)
        return {'confirmed': [], 'skipped': list(skipped.values()), 'remaining': 0}

    from patient_portal.api.serializers import PatientRecordSerializer
    from patient_portal.api.views import apply_patient_record_patch
    from omop_core.services.provisional_record import proposal_dates

    with transaction.atomic():
        locked = PatientRecord.objects.select_for_update().get(pk=record.pk)
        shown, marks = overlay(locked)
        confirmable, not_confirmable = classify(marks, getattr(shown, 'disease', None))
        group_of = {}
        for group in groups(marks, confirmable, not_confirmable):
            for member in group:
                group_of[member] = group
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

        # Decide each group whole, before any write.
        data = PatientRecordSerializer(shown).data if wanted else {}
        chosen_groups, seen = [], set()
        for field in wanted:
            group = group_of.get(field) or [field]
            if group[0] in seen:
                continue
            seen.update(group)
            if expected is not None:
                if any(m in expected and not _same(data.get(m), expected[m]) for m in group):
                    for m in group:
                        skip(m, CHANGED)
                    continue
                if any(m not in expected for m in group):
                    for m in group:
                        skip(m, NOT_SEEN)
                    continue
            chosen_groups.append(group)

        confirmed = []
        if chosen_groups:
            when = proposal_dates(person, locked, [m for g in chosen_groups for m in g if m in confirmable],
                                  {m: data.get(m) for g in chosen_groups for m in g})
            while chosen_groups:
                patch, refused = _validated(locked, {m: data.get(m) for g in chosen_groups for m in g})
                if refused:
                    for field, detail in refused.items():
                        skip(field, INVALID, detail)
                    gone = [g for g in chosen_groups if any(m in refused for m in g)]
                    for group in gone:
                        for m in group:
                            skip(m, INVALID, 'Not confirmed together with ' + ', '.join(sorted(refused)) + '.')
                    chosen_groups = [g for g in chosen_groups if g not in gone]
                    continue
                try:
                    with transaction.atomic():      # a savepoint: a group nothing acknowledged is taken back
                        apply_patient_record_patch(person, locked, patch, actor=actor, as_of=when,
                                                  revision_source=RecordRevision.SOURCE_CONFIRMED_FROM_PROPOSAL)
                        locked.refresh_from_db()
                        pinned = {m for m in patch if m in (locked.user_edited_fields or [])}
                        if pinned:
                            raise _Unprojected(pinned)
                except _Unprojected as unprojected:
                    locked.refresh_from_db()
                    dropped = [g for g in chosen_groups if unprojected.fields & set(g)]
                    for group in dropped:
                        for m in group:
                            skip(m, NO_PROJECTION)
                    chosen_groups = [g for g in chosen_groups if g not in dropped]
                    continue
                for field in patch:
                    if _holds_value(locked, field):
                        confirmed.append(field)
                    else:
                        skip(field, NOT_SAVED)
                break
        after = overlay(locked)[1]
        remaining = len(after)
    result = [item for field, item in skipped.items() if field not in confirmed]
    mark_dropped(result, after, confirmed)
    return {'confirmed': sorted(confirmed), 'skipped': result, 'remaining': remaining}


# A proposal the write's mirror throws away with the group it belongs to (a disease's slug and
# dates, as when the disease is typed) is reported as dropped, not as the reason it was not written.
DISCARDABLE = frozenset({NOT_SHOWN, LIST_FIELD, NO_PROJECTION})


def mark_dropped(skipped, marks_after, confirmed) -> None:
    """Relabel, in place, the skipped proposals the write took away with the group it confirmed.

    The mirror of a confirmed field also discards the proposals of the fields that are one
    answer with it (confirming a disease takes its slug, its dates and its ``disease_slugs``
    list, exactly as typing the disease does). Such a field was not confirmed and is no longer
    shown: ``dropped_with_group``, rather than the reason it was not written.
    """
    if not confirmed:
        return
    for item in skipped:
        if item['reason'] in DISCARDABLE and item['field'] not in marks_after:
            item['reason'] = DROPPED_WITH_GROUP


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
