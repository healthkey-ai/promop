"""
Authorization helpers for patient data access.

Three access paths checked in order:
1. Self-access (Identity → PatientUser → person_id matches target)
2. Personal representative (Identity → PersonalRepresentative → person_id, verified only)
3. Professional access (Identity → GroupAccess → group ∩ patient's groups
   OR org holding patient's record, non-expired, role-filtered to org_admin
   and doctor), including the same scoped organization-admin trusts used by
   the patient list. An analyst grant opens nothing here (#2416).
"""
from django.db import models
from django.utils import timezone

from .models import (
    GroupAccess,
    PersonalRepresentative,
)
from .services.access import CURATION_ROLES, PROFESSIONAL_ROLES

# One inventory of the role split (#2398, #2416): the curation roles read and
# write a patient. An analyst reaches no patient data at all -- the role is
# code mapping and PRism analytics -- so it is in neither set.
_READ_ROLES = frozenset(CURATION_ROLES)
_WRITE_ROLES = frozenset(CURATION_ROLES)
assert 'analyst' not in _READ_ROLES, 'an analyst never reads patient data (#2416)'
assert not set(PROFESSIONAL_ROLES) - _READ_ROLES - {'analyst'}


def _administers_patient(actor_identity, target_person_id: int) -> bool:
    from .services.access import get_admin_orgs

    return get_admin_orgs(actor_identity).filter(
        patients__person_id=target_person_id,
    ).exists()


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
    ).exists() or _administers_patient(actor_identity, target_person_id)


def can_write_patient(actor_identity, target_person_id: int) -> bool:
    """Return True if the actor may write (create/update/delete) patient data.

    Analysts have read-only access unless separately granted admin authority.
    Doctors and org_admins, including scoped trust admins, may write.
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
    ).exists() or _administers_patient(actor_identity, target_person_id)


def get_actor_role(actor_identity, target_person_id: int) -> str | None:
    """Return the actor's role relative to the target patient.

    Returns: 'self', 'representative', 'org_admin', 'doctor', or None. An
    analyst has no role toward any patient (#2416).
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

    if (not getattr(actor_identity, 'is_staff', False)
            and _administers_patient(actor_identity, target_person_id)):
        return 'org_admin'

    grant = _professional_grants(
        actor_identity, target_person_id, _READ_ROLES,
    ).first()
    if grant:
        return grant.role

    return None
