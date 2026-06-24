from django.db.models import Q, QuerySet
from django.utils import timezone

from omop_core.models import GroupAccess, Organization


def get_visible_orgs(user) -> QuerySet:
    """Return the orgs whose patients the user is allowed to see.

    - is_staff → all orgs
    - org_admin grant → their org(s)
    - doctor/navigator grant → the org of each assigned group
    - no grants → empty queryset
    """
    if getattr(user, 'is_staff', False):
        return Organization.objects.all()

    now = timezone.now()
    active = GroupAccess.objects.filter(
        identity=user,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )

    org_ids = set(
        active.filter(role='org_admin')
              .values_list('org_id', flat=True)
    )
    group_org_ids = set(
        active.exclude(role='org_admin')
              .values_list('group__organization_id', flat=True)
    )
    group_org_ids.discard(None)
    all_ids = org_ids | group_org_ids

    if not all_ids:
        return Organization.objects.none()

    return Organization.objects.filter(id__in=all_ids)
