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


def resolve_or_create_person(identity, email=None, allow_create=True):
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

    email = (email or getattr(identity, 'email', None) or "").strip()
    if email:
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
            # Re-point any existing PatientUser for this person to the current
            # identity. Needed when the Firebase emulator restarts and issues a
            # new UID for the same email: the old PatientUser row stays in the
            # DB (person unique constraint) but its identity is now stale.
            PatientUser.objects.update_or_create(
                person=person,
                defaults={"identity": identity},
            )
            return person

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
                email=email or None,
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
