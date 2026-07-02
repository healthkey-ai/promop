"""Shared service functions for patient_portal."""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from omop_core.models import PatientInfo, Person
from omop_core.services.pk import next_pk
from patient_portal.models import PatientUser

logger = logging.getLogger(__name__)


def resolve_or_create_person(identity, email=None):
    """Resolve an existing Person for *identity*, or auto-provision one.

    Lookup order:
      1. Existing PatientUser link
      2. PatientInfo whose email matches
      3. Brand-new Person + PatientUser (+ PatientInfo if email known)

    Returns the linked Person.
    """
    pu = PatientUser.objects.filter(identity=identity).first()
    if pu:
        return pu.person

    email = (email or identity.email or "").strip()
    if email:
        email_qs = PatientInfo.objects.filter(email=email)
        # Guard against cross-org collision: if multiple patients share the
        # same email, skip the email match and auto-provision a new person
        # rather than silently linking to the wrong patient.
        pi = email_qs.first() if email_qs.count() == 1 else None
        if pi:
            # Re-point any existing PatientUser for this person to the current
            # identity. Needed when the Firebase emulator restarts and issues a
            # new UID for the same email: the old PatientUser row stays in the
            # DB (person unique constraint) but its identity is now stale.
            PatientUser.objects.update_or_create(
                person=pi.person,
                defaults={"identity": identity},
            )
            return pi.person

    try:
        with transaction.atomic():
            new_id = next_pk(Person, 'person_id')
            person = Person.objects.create(
                person_id=new_id,
                year_of_birth=1900,
                gender_source_value="unknown",
                race_source_value="unknown",
                ethnicity_source_value="unknown",
            )
            if email:
                PatientInfo.objects.create(person=person, email=email)
            PatientUser.objects.create(identity=identity, person=person)
    except IntegrityError:
        pu = PatientUser.objects.filter(identity=identity).select_related('person').first()
        if pu:
            return pu.person
        raise

    logger.debug("auto-provisioned new Person for new Identity")
    return person
