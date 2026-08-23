"""
Authorization helpers for patient data access.

Three access paths checked in order:
1. Self-access (Identity → PatientUser → person_id matches target)
2. Personal representative (Identity → PersonalRepresentative → person_id, verified only)
3. Professional access (Identity → GroupAccess → group ∩ patient's groups
   OR org holding patient's record, non-expired, role-filtered)
"""
from django.db import models
from django.utils import timezone

from .models import (
    GroupAccess,
    PersonalRepresentative,
)

_READ_ROLES = frozenset({'org_admin', 'doctor', 'analyst'})
_WRITE_ROLES = frozenset({'org_admin', 'doctor'})


def _professional_grants(actor_identity, target_person_id: int, roles):
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

    # 3. Professional access. Role-filtered so a patient's own
    # GroupAccess(role='patient') never grants org-wide visibility.
    return _professional_grants(
        actor_identity, target_person_id, _READ_ROLES,
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
    return _professional_grants(
        actor_identity, target_person_id, _WRITE_ROLES,
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

    grant = _professional_grants(
        actor_identity, target_person_id, _READ_ROLES,
    ).first()
    if grant:
        return grant.role

    return None
