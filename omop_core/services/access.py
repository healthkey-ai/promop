from django.db.models import Q, QuerySet
from django.utils import timezone

from omop_core.models import GroupAccess, Organization, OrgTrust


def get_visible_orgs(user) -> QuerySet:
    """Return the orgs whose patients the user is allowed to see.

    Access is always explicit — there are no open orgs. Access is granted via:
      - is_staff → all active orgs
      - GroupAccess (org_admin, doctor, navigator) → specific orgs/groups
      - OrgTrust (domain or org-to-org) → orgs that trust the user's email domain
        or that trust an org the user already belongs to

    If a user has no matching grant and no matching trust, they see nothing.
    """
    if getattr(user, 'is_staff', False):
        return Organization.objects.all()

    now = timezone.now()
    active = GroupAccess.objects.filter(
        identity=user,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )

    # Direct GroupAccess grants
    direct_ids = set(
        active.filter(role='org_admin')
              .values_list('org_id', flat=True)
    )
    group_org_ids = set(
        active.exclude(role='org_admin')
              .values_list('group__organization_id', flat=True)
    )
    group_org_ids.discard(None)
    direct_ids |= group_org_ids

    # Org-to-org trusts: orgs that trust any org the user already belongs to
    trusted_by_org = set(
        OrgTrust.objects.filter(trusted_org_id__in=direct_ids)
                        .values_list('granting_org_id', flat=True)
    ) if direct_ids else set()

    # Domain trusts: orgs that trust the user's email domain
    user_domain = (getattr(user, 'email', '') or '').split('@')[-1]
    trusted_by_domain = set(
        OrgTrust.objects.filter(trusted_domain=user_domain)
                        .values_list('granting_org_id', flat=True)
    ) if user_domain else set()

    all_ids = direct_ids | trusted_by_org | trusted_by_domain

    if not all_ids:
        return Organization.objects.none()

    return Organization.objects.filter(id__in=all_ids)
