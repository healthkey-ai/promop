"""Give a verified account the Person an importer provisioned for its address.

Self-signup mints a Person eagerly, before the address is anything but a claim,
and every later path short-circuits on the `PatientUser` link. So the adoption
`resolve_or_create_person` already performs on an IdP login never gets a chance
on a local account: the patient ends up with an empty second Person while their
imported history stays on the provisioned one, and the address becomes ambiguous
for good (#1817).

The match runs here, at email verification, not at signup. At signup the address
is self-asserted, so matching on it would hand a stranger's clinical history to
anyone who types their email -- the hole `services.py` records closing. Only
`Identity.email_verified_at` is proof, and a trusted server-to-server caller's
assertion is not: the importer reads the address out of a vendor EHR export and
never verifies it either.

Two Persons are only joined when one of them is provably empty. A populated
placeholder is left exactly where it is and a `PersonMergeCandidate` is written
for a human, because joining two populated records is a clinical-safety
decision. Same stance as `prolog_surveys.ParticipantMergeCandidate`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction

from omop_core.models import PatientRecord, Person, PersonMergeCandidate
from patient_portal.models import PatientUser

logger = logging.getLogger(__name__)

# The read model every signup creates. Everything else in PATIENT_TABLES is
# either clinical or entered by the patient, so a row there means the account
# has data of its own and must not be merged automatically. Deriving the list
# instead of naming the tables means a new patient table counts from the day it
# lands, and ``tests/test_instance_data.py`` guards that it is classified.
_ALWAYS_PRESENT = frozenset({PatientRecord})


@dataclass(frozen=True)
class AdoptionOutcome:
    """What verification did about the provisioned Person, for logs and tests."""
    person_id: int | None = None
    adopted: bool = False
    merge_candidate: bool = False


def person_has_own_data(person_id: int) -> bool:
    """Whether this Person holds anything beyond the bare signup record."""
    # Imported here: patient_transfer pulls in every patient table, and the
    # verification endpoint should not pay that at import time.
    from omop_core.services.patient_transfer import PATIENT_TABLES

    for table in PATIENT_TABLES:
        if table.model in _ALWAYS_PRESENT:
            continue
        if table.parent is not None:
            # Its rows hang off a row of the parent table, which is checked
            # before it in that list, so a person with none there has none here.
            continue
        if table.model.objects.filter(**{table.lookup: person_id}).exists():
            return True
    return False


def _provisioned_candidate(email: str, exclude_person_id: int | None) -> Person | None:
    """The one unclaimed Person holding *email*, or None when that is not one.

    Preferring the unclaimed one is what lets this work on an address that has
    already forked: a Person somebody is logged in to is never a candidate, so
    one claimed duplicate beside one provisioned record still settles. Two
    unclaimed candidates do not, and no guess is made.
    """
    candidates = Person.objects.filter(email__iexact=email)
    if exclude_person_id is not None:
        candidates = candidates.exclude(person_id=exclude_person_id)
    claimed = set(
        PatientUser.objects
        .filter(person__in=candidates)
        .values_list('person_id', flat=True)
    )
    unclaimed = [p for p in candidates if p.person_id not in claimed]
    if len(unclaimed) != 1:
        if unclaimed:
            logger.warning(
                "email %s has %d unclaimed persons; not adopting any",
                email, len(unclaimed),
            )
        return None
    return unclaimed[0]


def _carry_over(placeholder: Person, provisioned: Person) -> None:
    """Move what signup knew onto the provisioned Person, filling only blanks.

    The importer's own values always win: it read them from a clinical source,
    while these were typed into a signup form.
    """
    updates = {
        field: getattr(placeholder, field)
        for field in ('given_name', 'family_name')
        if getattr(placeholder, field) and not getattr(provisioned, field)
    }
    if updates:
        for field, value in updates.items():
            setattr(provisioned, field, value)
        provisioned.save(update_fields=list(updates))

    org_id = (
        PatientRecord.objects
        .filter(person=placeholder)
        .values_list('organization_id', flat=True)
        .first()
    )
    if org_id:
        PatientRecord.objects.filter(
            person=provisioned, organization__isnull=True,
        ).update(organization_id=org_id)


def adopt_provisioned_person(identity) -> AdoptionOutcome:
    """Link a freshly verified account to its provisioned Person, if there is one.

    Call inside the transaction that recorded the verification. Does nothing
    unless the address is verified, exactly one unclaimed Person holds it, and
    the account's own Person is empty.
    """
    from omop_core.services.patient_transfer import PatientCopyError, delete_patient

    email = (identity.email or '').strip()
    if not email or not identity.has_verified_email:
        return AdoptionOutcome()

    link = PatientUser.objects.filter(identity=identity).first()
    candidate = _provisioned_candidate(email, link.person_id if link else None)
    if candidate is None:
        return AdoptionOutcome(person_id=link.person_id if link else None)

    with transaction.atomic():
        # Hold the row while re-reading the claim: two accounts verifying the
        # same address at once must not both adopt it.
        provisioned = Person.objects.select_for_update().get(pk=candidate.pk)
        if PatientUser.objects.filter(person=provisioned).exists():
            logger.warning(
                "person %s was claimed before identity %s could adopt it",
                provisioned.person_id, identity.pk,
            )
            return AdoptionOutcome(person_id=link.person_id if link else None)

        if link is None:
            PatientUser.objects.create(identity=identity, person=provisioned)
            return AdoptionOutcome(person_id=provisioned.person_id, adopted=True)

        placeholder = link.person
        if person_has_own_data(placeholder.person_id):
            PersonMergeCandidate.objects.get_or_create(
                claimed=placeholder, provisioned=provisioned,
                defaults={'email': email},
            )
            logger.info(
                "person %s holds data, leaving it with identity %s and filing a "
                "merge candidate against person %s",
                placeholder.person_id, identity.pk, provisioned.person_id,
            )
            return AdoptionOutcome(person_id=placeholder.person_id, merge_candidate=True)

        try:
            with transaction.atomic():
                _carry_over(placeholder, provisioned)
                link.person = provisioned
                link.save(update_fields=['person'])
                # delete_patient refuses while system data still points at the
                # row, so it has to run after the link moved -- and when
                # something else still does point at it (an EHR connection, an
                # invitation), the placeholder is a human's problem after all.
                delete_patient(placeholder.person_id)
        except PatientCopyError as exc:
            logger.warning(
                "person %s cannot be dropped (%s); filing a merge candidate against %s",
                placeholder.person_id, exc, provisioned.person_id,
            )
            PersonMergeCandidate.objects.get_or_create(
                claimed=placeholder, provisioned=provisioned,
                defaults={'email': email},
            )
            return AdoptionOutcome(person_id=placeholder.person_id, merge_candidate=True)
        logger.info(
            "identity %s adopted provisioned person %s, dropped empty person %s",
            identity.pk, provisioned.person_id, placeholder.person_id,
        )
        return AdoptionOutcome(person_id=provisioned.person_id, adopted=True)
