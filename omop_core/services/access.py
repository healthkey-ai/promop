from django.db.models import Q, QuerySet
from django.utils import timezone

from omop_core.models import GroupAccess, Organization, OrgTrust


def build_trusting_map(org_list) -> 'dict[int, set[int]]':
    """Return {org_id: set of granting_org_ids} for all orgs in org_list.

    Covers both trust modes:
    - Org-to-org: OrgTrust(granting_org=B, trusted_org=A) — B's patients accessible to A's users
    - Domain:     OrgTrust(granting_org=B, trusted_domain='x.com') — B's patients accessible to
                  @x.com users. Resolved to orgs by finding which orgs in org_list have users with
                  that email domain (via GroupAccess).
    """
    org_ids = [o.id for o in org_list]
    trusting_map: dict[int, set[int]] = {o.id: set() for o in org_list}

    if not org_ids:
        return trusting_map

    # Org-to-org trusts — exclude self-referential rows defensively
    for row in OrgTrust.objects.filter(
        trusted_org_id__in=org_ids,
        granting_org__is_active=True,
    ).values('trusted_org_id', 'granting_org_id'):
        if row['granting_org_id'] != row['trusted_org_id']:
            trusting_map[row['trusted_org_id']].add(row['granting_org_id'])

    # Domain trusts: find email domains of each org's users via GroupAccess
    org_user_domains: dict[int, set[str]] = {oid: set() for oid in org_ids}

    # org_admin grants have a direct org FK
    for row in GroupAccess.objects.filter(
        org_id__in=org_ids,
    ).values('org_id', 'identity__email'):
        email = row['identity__email'] or ''
        if '@' in email:
            org_user_domains[row['org_id']].add(email.split('@')[1].lower())

    # group-member grants go through group__organization
    for row in GroupAccess.objects.filter(
        group__organization_id__in=org_ids,
    ).values('group__organization_id', 'identity__email'):
        org_id = row['group__organization_id']
        email = row['identity__email'] or ''
        if '@' in email and org_id in org_user_domains:
            org_user_domains[org_id].add(email.split('@')[1].lower())

    # Invert: domain → orgs that have users with that domain
    domain_to_org_ids: dict[str, set[int]] = {}
    for org_id, domains in org_user_domains.items():
        for domain in domains:
            domain_to_org_ids.setdefault(domain, set()).add(org_id)

    all_domains = list(domain_to_org_ids.keys())
    if all_domains:
        for row in OrgTrust.objects.filter(
            trusted_domain__in=all_domains,
            granting_org__is_active=True,
        ).values('trusted_domain', 'granting_org_id'):
            domain = row['trusted_domain'].lower()
            for org_id in domain_to_org_ids.get(domain, set()):
                trusting_map[org_id].add(row['granting_org_id'])

    return trusting_map


def get_visible_orgs(user) -> QuerySet:
    """Return the orgs whose patients the user is allowed to see.

    Access is always explicit — there are no open orgs. Access is granted via:
      - is_staff → all active orgs
      - allows_public_aggregated_data → active orgs available to any authenticated user
      - GroupAccess (org_admin, doctor, navigator) → specific orgs/groups
      - OrgTrust (domain or org-to-org) → orgs that trust the user's email domain
        or that trust an org the user already belongs to

    If a user has no matching grant and no matching trust, they see nothing.
    """
    if getattr(user, 'is_staff', False):
        return Organization.objects.filter(is_active=True)

    now = timezone.now()
    active = GroupAccess.objects.filter(
        identity=user,
    ).filter(
        Q(expires_at__isnull=True) | Q(expires_at__gt=now)
    )

    # Direct org grants (org_admin, doctor, navigator)
    direct_ids = set(
        active.filter(org__isnull=False)
              .values_list('org_id', flat=True)
    )
    group_org_ids = set(
        active.exclude(role='org_admin')
              .values_list('group__organization_id', flat=True)
    )
    group_org_ids.discard(None)
    direct_ids |= group_org_ids

    # Org-to-org trusts: active orgs that trust any org the user already belongs to
    trusted_by_org = set(
        OrgTrust.objects.filter(
            trusted_org_id__in=direct_ids,
            granting_org__is_active=True,
        ).values_list('granting_org_id', flat=True)
    ) if direct_ids else set()

    # Domain trusts: active orgs that trust the user's email domain
    email = (getattr(user, 'email', '') or '')
    user_domain = email.split('@')[1] if '@' in email else ''
    trusted_by_domain = set(
        OrgTrust.objects.filter(
            trusted_domain=user_domain,
            granting_org__is_active=True,
        ).values_list('granting_org_id', flat=True)
    ) if user_domain else set()

    public_ids = set(
        Organization.objects.filter(
            is_active=True,
            allows_public_aggregated_data=True,
        ).values_list('id', flat=True)
    )

    all_ids = public_ids | direct_ids | trusted_by_org | trusted_by_domain

    if not all_ids:
        return Organization.objects.none()

    return Organization.objects.filter(id__in=all_ids)
