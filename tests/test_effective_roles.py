"""Role provenance must agree with scoped authorization, including admin trusts."""
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, OrgInvitation, OrgTrust, PatientGroup, PersonalRepresentative
from omop_core.services.access import get_admin_orgs
from patient_portal.api.serializers import UserSerializer
from patient_portal.models import Identity, PatientUser
from tests.factories import OrganizationFactory, PersonFactory

pytestmark = pytest.mark.django_db


def user(email='person@example.test', **kwargs):
    return Identity.objects.create_user(email=email, password=None, **kwargs)


def test_patient_and_professional_roles_are_both_reported_with_scope():
    actor = user()
    person = PersonFactory()
    PatientUser.objects.create(identity=actor, person=person)
    org = OrganizationFactory()
    group = PatientGroup.objects.create(name='Study group', organization=org)
    GroupAccess.objects.create(identity=actor, org=org, role='org_admin')
    GroupAccess.objects.create(identity=actor, group=group, role='doctor')
    data = UserSerializer(actor).data
    roles = data['effective_roles']
    assert {(r['role'], r['scope']) for r in roles} == {
        ('patient', 'patient'), ('org_admin', 'organization'), ('doctor', 'group'),
    }
    assert next(r for r in roles if r['role'] == 'patient')['person_id'] == person.pk
    assert next(r for r in roles if r['role'] == 'doctor')['group_name'] == 'Study group'
    assert {r['role'] for r in data['org_accesses']} == {'org_admin', 'doctor'}


@pytest.mark.parametrize('trust_type', ['organization', 'domain'])
def test_trust_admin_role_matches_authority_and_stays_scoped(trust_type):
    actor = user()
    source, target, unrelated = OrganizationFactory(), OrganizationFactory(), OrganizationFactory()
    if trust_type == 'organization':
        expiry = timezone.now() + timedelta(days=1)
        GroupAccess.objects.create(identity=actor, org=source, role='analyst', expires_at=expiry)
        OrgTrust.objects.create(granting_org=target, trusted_org=source)
    else:
        OrgTrust.objects.create(granting_org=target, trusted_domain='example.test')
    data = UserSerializer(actor).data
    inherited = [r for r in data['effective_roles'] if r['role'] == 'org_admin']
    assert len(inherited) == 1
    assert inherited[0]['org_slug'] == target.slug
    assert inherited[0]['source'] == f'{trust_type}_trust'
    assert set(get_admin_orgs(actor)) == {target}
    assert data['is_org_admin'] is True
    assert data['is_staff'] is False and data['is_superuser'] is False
    assert not any(r['role'] == 'staff' for r in data['effective_roles'])
    if trust_type == 'organization':
        assert inherited[0]['source_org_slug'] == source.slug
        assert inherited[0]['expires_at'] == expiry
    else:
        assert inherited[0]['source_domain'] == 'example.test'
    client = APIClient(); client.force_authenticate(user=actor)
    assert client.get(f'/api/orgs/{target.slug}/').status_code == 200
    assert client.get(f'/api/orgs/{unrelated.slug}/').status_code in {403, 404}
    assert client.post('/api/orgs/', {'name': 'Cannot create', 'slug': 'cannot-create'}, format='json').status_code == 403


def test_pending_invitation_cannot_enable_a_professional_role():
    actor = user()
    org = OrganizationFactory()
    OrgInvitation.objects.create(org=org, email=actor.email, role='org_admin',
        token='a'*64, expires_at=timezone.now()+timedelta(days=1))
    data = UserSerializer(actor).data
    assert data['effective_roles'] == []
    assert data['is_org_admin'] is False
    assert data['org_accesses'][0]['role'] is None
    assert data['org_accesses'][0]['pending_role'] == 'org_admin'


def test_pending_invitation_does_not_relabel_existing_grant():
    actor = user()
    org = OrganizationFactory()
    GroupAccess.objects.create(identity=actor, org=org, role='doctor')
    OrgInvitation.objects.create(org=org, email=actor.email, role='org_admin',
        token='b'*64, expires_at=timezone.now()+timedelta(days=1))
    data = UserSerializer(actor).data
    assert [r['role'] for r in data['effective_roles']] == ['doctor']
    assert [a['access_via'] for a in data['org_accesses']] == [['explicit_grant'], ['invitation_pending']]


def test_expired_source_grant_removes_trust_authority_and_reported_role():
    actor = user('person@untrusted.test')
    source, target = OrganizationFactory(), OrganizationFactory()
    GroupAccess.objects.create(identity=actor, org=source, role='doctor',
        expires_at=timezone.now()-timedelta(seconds=1))
    OrgTrust.objects.create(granting_org=target, trusted_org=source)
    assert not get_admin_orgs(actor).exists()
    assert UserSerializer(actor).data['effective_roles'] == []


@pytest.mark.parametrize('trust_type', ['organization', 'domain'])
def test_patient_only_membership_does_not_inherit_admin(trust_type):
    actor = user()
    source, target = OrganizationFactory(), OrganizationFactory()
    GroupAccess.objects.create(identity=actor, org=source, role='patient')
    OrgTrust.objects.create(granting_org=target, **(
        {'trusted_org': source} if trust_type == 'organization' else {'trusted_domain': 'example.test'}))
    assert not get_admin_orgs(actor).exists()
    assert all(r['role'] == 'patient' for r in UserSerializer(actor).data['effective_roles'])


def test_verified_delegation_is_separate_from_application_roles():
    actor = user()
    PersonalRepresentative.objects.create(representative=actor, person_id=123,
        relationship='guardian', verification_status='VERIFIED')
    PersonalRepresentative.objects.create(representative=actor, person_id=456,
        relationship='caregiver', verification_status='PENDING')
    data = UserSerializer(actor).data
    assert data['effective_roles'] == []
    assert data['patient_delegations'] == [{'person_id': 123, 'relationship': 'guardian'}]


def test_operational_superuser_has_staff_role_without_extra_application_mode():
    actor = Identity.objects.create_superuser(email='ops@example.test', password=None)
    data = UserSerializer(actor).data
    assert data['is_superuser'] is True
    assert data['effective_roles'] == [{'role': 'staff', 'scope': 'platform',
        'source': 'staff_flag', 'expires_at': None}]


def test_all_sources_remain_visible_for_the_same_admin_role():
    actor = user()
    source, target = OrganizationFactory(), OrganizationFactory()
    GroupAccess.objects.create(identity=actor, org=source, role='doctor')
    GroupAccess.objects.create(identity=actor, org=target, role='org_admin')
    OrgTrust.objects.create(granting_org=target, trusted_org=source)
    OrgTrust.objects.create(granting_org=target, trusted_domain='example.test')
    roles = UserSerializer(actor).data['effective_roles']
    assert {r['source'] for r in roles if r.get('org_slug') == target.slug} == {
        'org_grant', 'organization_trust', 'domain_trust',
    }
    assert set(get_admin_orgs(actor)) == {target}


def test_organization_trust_does_not_expand_recursively():
    actor = user('person@untrusted.test')
    source, target, third = OrganizationFactory(), OrganizationFactory(), OrganizationFactory()
    GroupAccess.objects.create(identity=actor, org=source, role='doctor')
    OrgTrust.objects.create(granting_org=target, trusted_org=source)
    OrgTrust.objects.create(granting_org=third, trusted_org=target)
    assert set(get_admin_orgs(actor)) == {target}
    assert {r['org_slug'] for r in UserSerializer(actor).data['effective_roles']
            if r['role'] == 'org_admin'} == {target.slug}
