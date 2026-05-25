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

    email = email or identity.email or ""
    if email:
        pi = PatientInfo.objects.filter(email=email).first()
        if pi:
            PatientUser.objects.get_or_create(
                identity=identity, defaults={"person": pi.person},
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

    logger.info(
        "auto-provisioned Person %d for identity pk=%d", new_id, identity.pk,
    )
    return person
