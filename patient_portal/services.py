"""Shared service functions for patient_portal."""
from __future__ import annotations

import logging

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from omop_core.models import GroupAccess, PatientRecord, Person
from omop_core.services.pk import next_pk
from patient_portal.models import Identity, PasswordHistory, PatientUser

logger = logging.getLogger(__name__)


def password_validation_errors(password, email=None):
    """Return a list of validation-error messages for a proposed password.

    Empty list means the password satisfies the project's AUTH_PASSWORD_VALIDATORS
    (minimum length, not-common, not-all-numeric, not-similar-to-email). Self-service
    password-set paths (signup, invite accept) MUST call this — Django does not run the
    validators automatically on set_password()/create_user() (PHR-S FM TI.1.1#06).
    """
    # A transient (unsaved) Identity lets UserAttributeSimilarityValidator compare the
    # password against the email without needing the account to exist yet.
    user = Identity(email=email) if email else None
    try:
        validate_password(password, user=user)
    except ValidationError as exc:
        return list(exc.messages)
    return []


def _reuse_candidates(identity):
    """Password-history rows that the no-reuse policy must check against:
    the most recent PASSWORD_HISTORY_SIZE (TI.1.1#05) plus anything set within
    PASSWORD_REUSE_DAYS (TI.1.1#04)."""
    qs = PasswordHistory.objects.filter(identity=identity).order_by('-created_at')
    recent_ids = list(qs.values_list('id', flat=True)[: settings.PASSWORD_HISTORY_SIZE])
    cutoff = timezone.now() - timedelta(days=settings.PASSWORD_REUSE_DAYS)
    windowed_ids = list(qs.filter(created_at__gte=cutoff).values_list('id', flat=True))
    keep_ids = set(recent_ids) | set(windowed_ids)
    return PasswordHistory.objects.filter(id__in=keep_ids)


def password_reuse_error(identity, raw_password):
    """Return an error message if raw_password reuses the current or a recent
    password (per PASSWORD_HISTORY_SIZE / PASSWORD_REUSE_DAYS), else None."""
    if identity.has_usable_password() and identity.check_password(raw_password):
        return 'You cannot reuse your current password.'
    for entry in _reuse_candidates(identity):
        if check_password(raw_password, entry.password):
            return 'You cannot reuse a recently used password.'
    return None


def record_password(identity):
    """Append the identity's current password hash to history and prune entries
    no longer needed for either reuse check."""
    if not identity.has_usable_password():
        return
    PasswordHistory.objects.create(identity=identity, password=identity.password)
    keep_ids = set(_reuse_candidates(identity).values_list('id', flat=True))
    PasswordHistory.objects.filter(identity=identity).exclude(id__in=keep_ids).delete()


def set_new_password(identity, raw_password, must_change=False):
    """Set a password and update all auth-policy state: record history, clear the
    force-change flag (unless must_change), and reset lockout counters."""
    identity.set_password(raw_password)
    identity.must_change_password = must_change
    identity.failed_login_count = 0
    identity.locked_until = None
    identity.save(update_fields=['password', 'must_change_password', 'failed_login_count', 'locked_until'])
    record_password(identity)


def patient_person_for(identity):
    """Return the Person this identity owns as a PHR Account Holder, or None.

    An identity is a *patient* (PHR Account Holder, per HL7 PHR-S FM PH.1) when
    it has a PatientUser link AND is not acting in any provider capacity —
    i.e. it holds no active provider GroupAccess grant and is not staff/superuser.
    Providers are scoped by org/group and are never treated as patients, even
    if a PatientUser row happens to exist for them.

    This is the canonical "is this a patient identity, and which record is
    theirs" test used by the API (UserSerializer) and frontend routing.
    """
    if identity is None or not getattr(identity, 'is_authenticated', False):
        return None
    if getattr(identity, 'is_staff', False):
        return None

    pu = (
        PatientUser.objects
        .filter(identity=identity)
        .select_related('person')
        .first()
    )
    if pu is None:
        return None

    now = timezone.now()
    has_provider_grant = GroupAccess.objects.filter(
        identity=identity,
    ).exclude(
        role='patient',
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now),
    ).exists()
    if has_provider_grant:
        return None

    return pu.person


def resolve_or_create_person(identity, email=None, allow_create=True, email_verified=None):
    """Resolve an existing Person for *identity*, or auto-provision one.

    Lookup order:
      1. Existing PatientUser link
      2. Person whose HealthKey email extension matches
      3. Brand-new Person + PatientUser (+ PatientRecord if email known)

    When allow_create=False, steps 1 and 2 still run (an existing patient is
    always returned) but step 3 is skipped — returns None instead of creating.

    Returns the linked Person, or None if not found and allow_create=False.
    """
    pu = PatientUser.objects.filter(identity=identity).first()
    if pu:
        return pu.person

    if email_verified is None:
        email_verified = getattr(identity, 'is_local', False)

    email = (email or getattr(identity, 'email', None) or "").strip()
    if email and email_verified:
        person_qs = Person.objects.filter(email=email)
        # Legacy fallback while older PatientRecord snapshots still exist.
        email_qs = PatientRecord.objects.filter(email=email)
        # Guard against cross-org collision: if multiple patients share the
        # same email, skip the email match and auto-provision a new person
        # rather than silently linking to the wrong patient.
        if person_qs.count() == 1:
            person = person_qs.first()
        else:
            pi = email_qs.first() if email_qs.count() == 1 else None
            person = pi.person if pi else None
        if person:
            holder = PatientUser.objects.filter(person=person).first()
            if holder is None:
                PatientUser.objects.create(identity=identity, person=person)
                return person
            if holder.identity_id == identity.pk:
                return person
            logger.warning(
                "refusing to rebind PatientUser for person %s from identity %s to %s",
                person.person_id,
                holder.identity_id,
                identity.pk,
            )

    if not allow_create:
        return None

    try:
        with transaction.atomic():
            new_id = next_pk(Person, 'person_id')
            person = Person.objects.create(
                person_id=new_id,
                year_of_birth=1900,
                gender_source_value="unknown",
                race_source_value="unknown",
                ethnicity_source_value="unknown",
                # Only a verified address may be stamped on the new Person.
                # The gate above governs the *lookup*; without this it would
                # still persist an unverified claim, letting anyone who can
                # register at the IdP plant a Person row keyed on someone
                # else's address. The real owner's later verified sign-in then
                # sees two Persons for that email, trips the cross-org
                # collision guard, and is forked into a duplicate instead of
                # linking to their own record.
                email=email if email_verified else None,
            )
            PatientRecord.objects.create(person=person)
            PatientUser.objects.create(identity=identity, person=person)
            from omop_core.services.patient_record_service import refresh_patient_record
            refresh_patient_record(person)
    except IntegrityError:
        pu = PatientUser.objects.filter(identity=identity).select_related('person').first()
        if pu:
            return pu.person
        raise

    logger.debug("auto-provisioned new Person for new Identity")
    return person


# ---------------------------------------------------------------------------
# PROlog survey runner (prolog_surveys)
# ---------------------------------------------------------------------------

def prolog_participant_id(request):
    """PROLOG_PARTICIPANT_RESOLVER: the signed-in patient's person_id, or None.

    Returns None for anyone who is not a PHR Account Holder — staff, providers,
    service tokens, anonymous callers — so a provider trying a survey never has
    it recorded against a patient they can see.

    What None then means depends on the instrument, and the difference is worth
    being precise about: on an anonymous survey (or for an invited respondent)
    the runner mints an unidentified person for the response, see
    create_unidentified_person. On any other survey the runner refuses the
    caller outright — its _check_access requires an authenticated caller the
    resolver recognises — so a staff member cannot take an account survey at
    all. If they need to, the instrument has to be anonymous, or this resolver
    needs a path of its own.
    """
    person = patient_person_for(getattr(request, 'user', None))
    return person.person_id if person is not None else None


def create_unidentified_person(source='prolog'):
    """Create a Person with no Identity, no PatientUser and no demographics.

    The counterpart to resolve_or_create_person, which provisions a person *for
    an identity*. This one mints a person who is not anyone yet: the subject of
    a survey response that may never be claimed. PROlog binds every response to
    a person (DEP-2/RUN-2), and "anonymous" means this person carries nothing
    that could name them — not that no record exists.

    Three things this deliberately does:

    * **Creates the PatientRecord.** Issue #883 is this same primitive built
      without one: a Person created through find_or_create has no record, so
      /api/v1/patient-records/<id>/refresh/ answers 404 and the patient is
      underivable with no API call that can fix it. Thirty-nine patients ended
      that way in the 2026-08-31 migration.
    * **Does not run derivation.** There is nothing clinical to derive for a
      person who has only just been minted, and refresh is expensive.
    * **Sets no demographics.** resolve_or_create_person writes
      year_of_birth=1900 and "unknown" source values because it is provisioning
      a patient. This person is not a patient yet, and a placeholder birth year
      is an identifying attribute that is also false.

    Callers that later learn who this is promote the same row in place — an
    Identity and a PatientUser are attached to it — so no answer moves and no
    second person appears.
    """
    with transaction.atomic():
        person = Person.objects.create(person_id=next_pk(Person, 'person_id'))
        PatientRecord.objects.create(person=person)
    logger.info('minted unidentified person %s for %s', person.person_id, source)
    return person
