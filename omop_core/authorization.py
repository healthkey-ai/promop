"""
Authorization helpers for patient data access.

Three access paths checked in order:
1. Self-access (Identity → PatientUser → person_id matches target)
2. Personal representative (Identity → PersonalRepresentative → person_id, verified only)
3. Professional access (Identity → GroupAccess → the patient's group *or* org, non-expired)

Every professional check goes through ``professional_grants``. It used to be
written out at each call site, and the copies had drifted: reading and role
lookup matched only group grants while writing matched group *and* org, so an
org-level grant permitted a write its holder could not read back.
"""
from django.db import models
from django.utils import timezone

from .models import (
    GroupAccess,
    PersonalRepresentative,
)

#: Roles that make someone a professional with respect to a patient. Note what
#: is absent: ``patient``. That role is how an identity is marked as belonging
#: to an organization, not as having any standing over its other patients — a
#: check that treated it as a grant would let one patient read every other
#: patient in their org. Their own record reaches them through self-access.
PROFESSIONAL_READ_ROLES = frozenset({'org_admin', 'doctor', 'analyst'})

#: Analysts read; they do not write.
PROFESSIONAL_WRITE_ROLES = frozenset({'org_admin', 'doctor'})

# Most privileged first. A grant can be held through more than one path, and a
# caller asking for "the" role wants the strongest one rather than whichever the
# database returned first.
_ROLE_PRECEDENCE = ['org_admin', 'doctor', 'analyst']


def professional_grants(actor_identity, target_person_id: int, roles):
    """Non-expired ``GroupAccess`` rows through which the actor reaches the patient.

    A grant reaches a patient either through a group the patient belongs to or
    through the organization that holds their record. Both are real grants; a
    check that honours only one of them is a bug, which is why this is in one
    place.

    ``roles`` is required rather than defaulting to "any". The permissive
    default is the one nobody should get by accident: at org scope it spans
    every patient the organization holds.
    """
    now = timezone.now()
    return GroupAccess.objects.filter(
        identity=actor_identity,
        role__in=roles,
    ).filter(
        models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now),
    ).filter(
        models.Q(
            group__memberships__person_id=target_person_id,
        ) | models.Q(
            org__patients__person_id=target_person_id,
        )
    )


def can_access_patient(actor_identity, target_person_id: int) -> bool:
    """Check if actor has access to target patient's data."""
    from patient_portal.models import PatientUser

    # No actor (e.g. an OAuth client_credentials token, whose AccessToken has no
    # resource owner) has no per-patient access — fail closed instead of
    # dereferencing None below.
    if actor_identity is None:
        return False

    if getattr(actor_identity, 'is_staff', False):
        return True

    # 1. Self-access
    try:
        if actor_identity.patient_user.person_id == target_person_id:
            return True
    except PatientUser.DoesNotExist:
        pass

    # 2. Personal representative (verified only)
    if PersonalRepresentative.objects.filter(
        representative=actor_identity,
        person_id=target_person_id,
        verification_status='VERIFIED',
    ).exists():
        return True

    # 3. Professional access. Analysts read but do not write, which is the only
    #    difference between this role set and can_write_patient's.
    return professional_grants(
        actor_identity, target_person_id, roles=PROFESSIONAL_READ_ROLES,
    ).exists()


def can_write_patient(actor_identity, target_person_id: int) -> bool:
    """Return True if the actor may write (create/update/delete) patient data.

    Analysts have read-only access; only doctors and org_admins may write.
    Self-access and personal representatives can always write their own data.
    """
    from patient_portal.models import PatientUser

    # No actor (e.g. a userless OAuth client_credentials token) may write.
    if actor_identity is None:
        return False

    if getattr(actor_identity, 'is_staff', False):
        return True

    # Patients can write their own record
    try:
        if actor_identity.patient_user.person_id == target_person_id:
            return True
    except PatientUser.DoesNotExist:
        pass

    # Verified personal representatives can write
    if PersonalRepresentative.objects.filter(
        representative=actor_identity,
        person_id=target_person_id,
        verification_status='VERIFIED',
    ).exists():
        return True

    # Professional access: only doctor / org_admin roles may write
    return professional_grants(
        actor_identity, target_person_id, roles=PROFESSIONAL_WRITE_ROLES,
    ).exists()


def get_actor_role(actor_identity, target_person_id: int) -> str | None:
    """Return the actor's role relative to the target patient.

    Returns: 'self', 'representative', 'org_admin', 'doctor', 'analyst', or None.
    """
    from patient_portal.models import PatientUser

    # No actor (e.g. a userless OAuth client_credentials token) has no role —
    # fail closed instead of dereferencing None below.
    if actor_identity is None:
        return None

    try:
        if actor_identity.patient_user.person_id == target_person_id:
            return 'self'
    except PatientUser.DoesNotExist:
        pass

    if PersonalRepresentative.objects.filter(
        representative=actor_identity,
        person_id=target_person_id,
        verification_status='VERIFIED',
    ).exists():
        return 'representative'

    roles = set(
        professional_grants(actor_identity, target_person_id, roles=PROFESSIONAL_READ_ROLES)
        .values_list('role', flat=True)
    )
    for role in _ROLE_PRECEDENCE:
        if role in roles:
            return role

    return None
