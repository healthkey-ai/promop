"""What a list may say about the account behind a patient (#2838).

A patient record can belong to somebody who also works here: an org admin
who added themselves as a patient (#2773), a doctor who is a patient of the
organization. Two lists say so, and both decide on the server:

- the patient list marks such a record (``other_roles_by_person``);
- an organization's access grants link a ``patient`` grant to the record
  (``patient_record_links``).

Which roles an account holds is access administration, not patient data, so
it is told only to whoever may already read it: staff, and the direct org
admins of the organization a role is granted at (``get_access_admin_orgs``,
the rule behind ``GET orgs/<slug>/access/``). Both functions make a fixed
number of queries whatever the number of rows.
"""
from django.db.models import Q
from django.utils import timezone

from omop_core.models import GroupAccess, PatientRecord

from .access import PROFESSIONAL_ROLES, get_access_admin_orgs

#: The order a record's other roles are listed in.
OTHER_ROLE_ORDER = ('staff',) + PROFESSIONAL_ROLES


def other_roles_by_person(viewer, person_ids) -> dict:
    """``{person_id: [role, ...]}`` for the patients whose account holds more.

    A patient appears only when their active sign-in link belongs to an
    account that holds another role the viewer may know about:

    - a staff viewer is told ``staff`` for a staff account, and every active
      professional grant (``org_admin``, ``doctor``, ``analyst``) at an active
      organization, organization-level or group-level;
    - any other viewer is told the organization-level professional grants at
      the organizations whose access it administers by a direct grant, and
      nothing else: not a grant at another organization, not staff status.

    A viewer who is neither gets an empty answer after one query. The caller
    decides whether the viewer is a person at all (a machine credential is
    not) and which rows may carry the answer.
    """
    from patient_portal.models import PatientUser

    wanted = {person_id for person_id in person_ids if person_id is not None}
    if viewer is None or not getattr(viewer, 'is_authenticated', False) or not wanted:
        return {}
    is_staff = bool(getattr(viewer, 'is_staff', False))
    admin_org_ids = set()
    if not is_staff:
        admin_org_ids = set(get_access_admin_orgs(viewer).values_list('id', flat=True))
        if not admin_org_ids:
            return {}

    person_by_identity = {}
    found = {}
    for person_id, identity_id, account_is_staff in PatientUser.objects.filter(
        person_id__in=wanted, is_active=True, identity__is_active=True,
    ).values_list('person_id', 'identity_id', 'identity__is_staff'):
        person_by_identity[identity_id] = person_id
        if is_staff and account_is_staff:
            found.setdefault(person_id, set()).add('staff')
    if not person_by_identity:
        return {}

    grants = GroupAccess.objects.filter(
        identity_id__in=person_by_identity, role__in=PROFESSIONAL_ROLES,
    ).filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
    if is_staff:
        grants = grants.filter(
            Q(org__is_active=True) | Q(group__organization__is_active=True))
    else:
        grants = grants.filter(org_id__in=admin_org_ids)
    for identity_id, role in grants.values_list('identity_id', 'role'):
        found.setdefault(person_by_identity[identity_id], set()).add(role)

    return {
        person_id: [role for role in OTHER_ROLE_ORDER if role in roles]
        for person_id, roles in found.items()
    }


def patient_record_links(viewer, grants) -> dict:
    """``{grant.id: person_id}`` for the patient grants the viewer can open.

    A ``patient`` grant says an account is a patient of the organization; its
    record is the person the account's active sign-in link names. The link is
    offered only for a record that exists and that the viewer may read
    (``accessible_person_ids``, which is ``can_access_patient``): a grant
    whose record is somewhere the viewer cannot reach stays plain text.
    """
    from omop_core.authorization import accessible_person_ids
    from patient_portal.models import PatientUser

    identity_ids = {grant.identity_id for grant in grants if grant.role == 'patient'}
    if viewer is None or not identity_ids:
        return {}
    person_by_identity = dict(PatientUser.objects.filter(
        identity_id__in=identity_ids, is_active=True,
    ).values_list('identity_id', 'person_id'))
    readable = accessible_person_ids(viewer, person_by_identity.values())
    if not readable:
        return {}
    openable = set(PatientRecord.objects.filter(
        person_id__in=readable,
    ).values_list('person_id', flat=True))
    return {
        grant.id: person_by_identity[grant.identity_id]
        for grant in grants
        if grant.role == 'patient' and person_by_identity.get(grant.identity_id) in openable
    }
