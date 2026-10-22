"""Data trusts must not confer authority to delegate organization access."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, OrgInvitation, OrgTrust, PatientGroup
from patient_portal.models import Identity
from tests.factories import OrganizationFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def access_setup():
    actor = Identity.objects.create_user(
        email='analyst@example.test', email_verified_at=timezone.now(),
    )
    org, source = OrganizationFactory(), OrganizationFactory()
    invitee = Identity.objects.create_user(email='invitee@example.test')
    grant = GroupAccess.objects.create(identity=invitee, org=org, role='analyst')
    invitation = OrgInvitation.objects.create(
        org=org, email=invitee.email, role='analyst', token='a' * 64,
        expires_at=timezone.now() + timedelta(days=1),
    )
    trust = OrgTrust.objects.create(granting_org=org, trusted_org=source)
    client = APIClient()
    client.force_authenticate(actor)
    return client, actor, org, source, grant, invitation, trust


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
@pytest.mark.parametrize('authority', [
    'analyst', 'doctor', 'domain_trust', 'organization_trust', 'trust_only',
    'other_org_admin', 'group_admin', 'expired_admin', 'inactive_org',
])
def test_access_administration_requires_explicit_target_admin(access_setup, prefix, authority):
    client, actor, org, source, grant, invitation, trust = access_setup
    if authority in {'analyst', 'doctor', 'domain_trust'}:
        GroupAccess.objects.create(
            identity=actor, org=org, role='doctor' if authority == 'doctor' else 'analyst',
        )
    if authority in {'domain_trust', 'trust_only', 'other_org_admin', 'group_admin', 'expired_admin'}:
        OrgTrust.objects.create(granting_org=org, trusted_domain='example.test')
    if authority in {'organization_trust', 'other_org_admin'}:
        GroupAccess.objects.create(identity=actor, org=source,
                                   role='org_admin' if authority == 'other_org_admin' else 'analyst')
    if authority == 'group_admin':
        group = PatientGroup.objects.create(name='Admin subgroup', organization=org)
        GroupAccess.objects.create(identity=actor, group=group, role='org_admin')
    if authority == 'expired_admin':
        GroupAccess.objects.create(identity=actor, org=org, role='org_admin',
                                   expires_at=timezone.now() - timedelta(seconds=1))

    if authority == 'inactive_org':
        GroupAccess.objects.create(identity=actor, org=org, role='org_admin')
        org.is_active = False
        org.save(update_fields=['is_active'])

    base = f'{prefix}/orgs/{org.slug}'
    with patch('patient_portal.api.org_views._send_invitation_email') as send:
        for method, path, data in [
            ('post', '/invite/', {'email': actor.email, 'role': 'org_admin'}),
            ('post', '/invite/', {'email': 'new@example.test', 'role': 'doctor'}),
            ('get', '/access/', {}),
            ('patch', f'/access/{grant.pk}/', {'role': 'doctor', 'is_premium': True}),
            ('delete', f'/access/{grant.pk}/', {}),
            ('get', '/invitations/', {}),
            ('delete', f'/invitations/{invitation.pk}/', {}),
            ('get', '/trusts/', {}),
            ('post', '/trusts/', {'trusted_domain': 'attacker.test'}),
            ('delete', f'/trusts/{trust.pk}/', {}),
        ]:
            response = getattr(client, method)(base + path, data, format='json')
            assert response.status_code == 403, (authority, method, path, response.data)
        send.assert_not_called()
    grant.refresh_from_db()
    grant.identity.refresh_from_db()
    invitation.refresh_from_db()
    assert grant.role == 'analyst'
    assert not grant.identity.is_premium
    assert invitation.cancelled_at is None
    assert OrgTrust.objects.filter(pk=trust.pk).exists()
    assert OrgInvitation.objects.count() == 1
    assert not Identity.objects.filter(email='new@example.test').exists()
    assert not GroupAccess.objects.filter(identity=actor, org=org, role='org_admin',
                                         expires_at__isnull=True, org__is_active=True).exists()
    detail = client.get(base + '/')
    if detail.status_code == 200:
        assert detail.data['can_manage_access'] is False


@pytest.mark.parametrize('prefix', ['/api', '/api/v1'])
@pytest.mark.parametrize('authority', ['org_admin', 'staff'])
def test_explicit_admin_and_staff_can_manage_access(access_setup, prefix, authority):
    client, actor, org, source, grant, invitation, trust = access_setup
    if authority == 'staff':
        actor.is_staff = True
        actor.save(update_fields=['is_staff'])
    else:
        GroupAccess.objects.create(identity=actor, org=org, role='org_admin',
                                   expires_at=timezone.now() + timedelta(days=1))
    base = f'{prefix}/orgs/{org.slug}'
    assert client.get(base + '/').data['can_manage_access'] is True
    listed = client.get(f'{prefix}/orgs/').data
    assert next(item for item in listed if item['slug'] == org.slug)['can_manage_access'] is True
    with patch('patient_portal.api.org_views._send_invitation_email') as send:
        response = client.post(base + '/invite/', {'email': 'new@example.test', 'role': 'analyst'}, format='json')
        assert response.status_code == 201, response.data
        send.assert_called_once()
    assert GroupAccess.objects.filter(identity__email='new@example.test', org=org, role='analyst').exists()
    assert client.get(base + '/access/').status_code == 200
    assert client.get(base + '/invitations/').status_code == 200
    assert client.patch(base + f'/access/{grant.pk}/', {'role': 'doctor'}, format='json').status_code == 200
    assert client.delete(base + f'/access/{grant.pk}/').status_code == 204
    assert client.delete(base + f'/invitations/{invitation.pk}/').status_code == 204
    assert client.get(base + '/trusts/').status_code == 200
    assert client.post(base + '/trusts/', {'trusted_domain': 'new.test'}, format='json').status_code == 201
    assert client.delete(base + f'/trusts/{trust.pk}/').status_code == 204
