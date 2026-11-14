"""The explicit answer to the pre-existing-conditions question (#1988).

``preexisting_conditions`` is the evidence (the categories a patient or clinician
recorded) and ``no_pre_existing_conditions`` is the answer to a separate question.
The flag cannot be derived from an empty list: an empty list is also what a record
carries before anyone asked. It therefore must not be stored alongside categories —
if both exist the list wins and the flag is invalidated, rather than the reverse.

``PreexistingConditionsAttestation`` records that a person explicitly answered
"none". It is the durable provenance the repair relies on: after the old formula
rows are cleared, an answer exists again only because someone gave it.
"""
from django.utils import timezone

from omop_core.models import Person, PreexistingConditionsAttestation

# Only the dedicated deliberate-answer action writes these. Legacy generic
# attestations may have been produced by full-form autosave echoes.
EXPLICIT_ATTESTATION_SOURCES = frozenset({
    'patient-explicit', 'clinician-explicit', 'proxy-explicit', 'host-explicit',
})


def condition_categories(value):
    """The recorded category codes, ignoring anything that is not a distinct code."""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value
            if isinstance(item, str) and str(item).strip()]


def contradicts_no_pre_existing_conditions(flag, categories):
    """True when an explicit "none" answer coexists with a category value.

    The same predicate as the 0305 CHECK, which permits only NULL, `[]` and
    JSON null beside True. A narrower test (distinct non-blank codes only) let
    `[""]`, `{}` or a bare string through validation to an IntegrityError: a
    500 where the client should get a 400 and choose.
    """
    return flag is True and categories is not None and categories != []


def _record_for(person):
    return getattr(person, 'patient_record', None)


def attest_no_preexisting_conditions(person, *, source='patient'):
    """Record the person's explicit "I have no pre-existing conditions" answer.

    Refuses when categories are recorded: the specific answer already exists and
    the blanket one would contradict it.
    """
    if person is None:
        raise ValueError('A pre-existing-conditions answer needs a patient.')
    record = _record_for(person)
    # The 0305 CHECK's predicate, not "distinct non-blank codes": a junk value
    # such as `[""]` or `{}` would otherwise reach the save as an
    # IntegrityError (#2489).
    if record is not None and contradicts_no_pre_existing_conditions(
            True, record.preexisting_conditions):
        raise ValueError(
            'Recorded pre-existing conditions contradict a "none" answer.')
    attestation, _ = PreexistingConditionsAttestation.objects.update_or_create(
        person=person,
        defaults={'confirmed_at': timezone.now(), 'source': source or 'patient'},
    )
    _write_flag(person, True)
    return attestation


def withdraw_no_preexisting_conditions(person):
    """Withdraw the answer; the flag becomes unknown, not "has conditions"."""
    if person is None:
        raise ValueError('A pre-existing-conditions answer needs a patient.')
    PreexistingConditionsAttestation.objects.filter(person=person).delete()
    _write_flag(person, None)


def is_no_preexisting_conditions_attested(person):
    return PreexistingConditionsAttestation.objects.filter(person=person).exists()


def sync_no_preexisting_conditions(record, *, source='patient'):
    """Make the attestation match the record's stored answer.

    Takes the record, not the person: the caller has just written and refreshed
    it, and ``person.patient_record`` can hand back a stale cached row. Called
    after a write to the flag or the categories, so the durable evidence cannot
    drift from the projection. A record with categories, with a cleared answer,
    or with an explicit "there are conditions" answer has no "none" attestation.
    """
    if record is None:
        return None
    if (record.no_pre_existing_conditions is True
            and not contradicts_no_pre_existing_conditions(
                True, record.preexisting_conditions)):
        return attest_no_preexisting_conditions(record.person, source=source)
    PreexistingConditionsAttestation.objects.filter(person_id=record.person_id).delete()
    return None


def supersede_no_preexisting_conditions(record):
    """Withdraw a blanket "none" answer when a category has been recorded.

    Called from the projection because a category can arrive through a path that
    does not go through the answer's own writer (admin, import, direct SQL).
    Any value the guard treats as a contradiction withdraws it, junk lists
    (`[""]`) included: the guard nulls the flag for those, and an attestation
    left behind would claim an answer the record no longer carries.
    """
    if record.preexisting_conditions not in (None, []):
        PreexistingConditionsAttestation.objects.filter(
            person_id=record.person_id).delete()


def _write_flag(person, value):
    record = _record_for(person)
    if record is None or record.no_pre_existing_conditions == value:
        return
    record.no_pre_existing_conditions = value
    record.save(update_fields=['no_pre_existing_conditions'])
