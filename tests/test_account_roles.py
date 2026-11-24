"""A patient whose account holds another role, and the Patient role's link (#2838).

The patient list marks such a record and an organization's access grants
link a patient grant to its record. Both are decided on the server, and both
are told only to whoever may already read them.
"""
from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.authorization import accessible_person_ids, can_access_patient
from omop_core.models import (
    GroupAccess, OrgTrust, PatientGroup, PatientGroupMembership, PersonalRepresentative,
)
from omop_core.services.access import get_access_admin_orgs, has_explicit_org_admin_access
from omop_core.services.account_roles import other_roles_by_person, patient_record_links
from patient_portal.models import Identity, PatientUser
from tests.factories import OrganizationFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

LIST = '/api/v1/patient-records/'
LEGACY_LIST = '/api/patient-info/'


def account(email, **kwargs):
    return Identity.objects.create_user(
        email=email, password=None, email_verified_at=timezone.now(), **kwargs)


def grant(identity, org, role, **kwargs):
    return GroupAccess.objects.create(identity=identity, org=org, role=role, **kwargs)


def patient(org, email=None, roles=(), role_org=None, **account_kwargs):
    """A record at ``org``; with ``email``, an account linked to it that holds
    the patient role there and ``roles`` at ``role_org`` (``org`` by default)."""
    record = PatientRecordFactory(organization=org)
    if email is None:
        return record
    identity = account(email, **account_kwargs)
    PatientUser.objects.create(identity=identity, person=record.person)
    grant(identity, org, 'patient')
    for role in roles:
        grant(identity, role_org or org, role)
    return record


def signed_in(identity):
    """A browser session: the one kind of reader the marker is for."""
    client = APIClient()
    client.force_login(identity)
    return client


def rows(response):
    assert response.status_code == 200, response.content[:300]
    data = response.data
    return {row['person_id']: row for row in (data['results'] if isinstance(data, dict) else data)}


def marked(response):
    return {person_id: row['other_roles'] for person_id, row in rows(response).items()
            if 'other_roles' in row}


@pytest.fixture
def home():
    return OrganizationFactory(name='Home', slug='home')


@pytest.fixture
def elsewhere():
    return OrganizationFactory(name='Elsewhere', slug='elsewhere')


@pytest.fixture
def people(home, elsewhere):
    """One patient per kind of account, all with a record at ``home``."""
    expired = patient(home, 'expired@example.test')
    grant(expired.person.portal_user.identity, home, 'doctor',
          expires_at=timezone.now() - timedelta(days=1))
    unlinked = patient(home, 'unlinked@example.test', roles=['doctor'])
    PatientUser.objects.filter(person=unlinked.person).update(is_active=False)
    disabled = patient(home, 'disabled@example.test', roles=['doctor'])
    Identity.objects.filter(email='disabled@example.test').update(is_active=False)
    return {
        'admin': patient(home, 'admin-patient@example.test', roles=['org_admin']),
        'doctor': patient(home, 'doctor-patient@example.test', roles=['doctor']),
        'analyst': patient(home, 'analyst-patient@example.test', roles=['analyst']),
        'both': patient(home, 'both-patient@example.test', roles=['analyst', 'doctor']),
        'staff': patient(home, 'staff-patient@example.test', is_staff=True),
        'away': patient(home, 'away-patient@example.test', roles=['doctor'], role_org=elsewhere),
        'plain': patient(home, 'plain-patient@example.test'),
        'no_account': patient(home),
        'expired': expired,
        'unlinked': unlinked,
        'disabled': disabled,
    }


def ids(people, *names):
    return {people[name].person_id for name in names}


# -- the patient list: who is marked ----------------------------------------


def test_staff_sees_every_other_role_and_staff_accounts(people):
    viewer = account('staff@example.test', is_staff=True)
    assert marked(signed_in(viewer).get(LIST)) == {
        people['admin'].person_id: ['org_admin'],
        people['doctor'].person_id: ['doctor'],
        people['analyst'].person_id: ['analyst'],
        people['both'].person_id: ['doctor', 'analyst'],
        people['staff'].person_id: ['staff'],
        people['away'].person_id: ['doctor'],
    }


def test_an_org_admin_sees_the_roles_granted_at_their_organization_only(home, people):
    viewer = account('org-admin@example.test')
    grant(viewer, home, 'org_admin')
    found = marked(signed_in(viewer).get(LIST))
    assert found == {
        people['admin'].person_id: ['org_admin'],
        people['doctor'].person_id: ['doctor'],
        people['analyst'].person_id: ['analyst'],
        people['both'].person_id: ['doctor', 'analyst'],
    }
    # Staff status is the platform's, and a grant elsewhere is that
    # organization's: an org admin is told neither.
    assert not ids(people, 'staff', 'away') & set(found)


def test_only_the_patient_role_no_account_an_expired_grant_or_a_dead_link_is_unmarked(home, people):
    unmarked = ids(people, 'plain', 'no_account', 'expired', 'unlinked', 'disabled')
    admin = account('org-admin@example.test')
    grant(admin, home, 'org_admin')
    for viewer in (account('staff@example.test', is_staff=True), admin):
        response = signed_in(viewer).get(LIST)
        assert unmarked <= set(rows(response))
        assert not unmarked & set(marked(response))


def test_a_grant_at_an_inactive_organization_is_not_a_role(home, elsewhere, people):
    elsewhere.is_active = False
    elsewhere.save(update_fields=['is_active'])
    viewer = account('staff@example.test', is_staff=True)
    assert people['away'].person_id not in marked(signed_in(viewer).get(LIST))


def test_a_public_demo_orgs_signup_grant_is_not_a_role():
    """Every self-signup at a demo org gets a record and a ``doctor`` grant
    to browse the samples: a sandbox credential, not somebody who works there."""
    demo = OrganizationFactory(name='Demo', slug='demo', allows_patient_signup=True)
    signup = patient(demo, 'signup@example.test', roles=['doctor', 'analyst'])
    group = PatientGroup.objects.create(name='Demo group', organization=demo)
    GroupAccess.objects.create(
        identity=signup.person.portal_user.identity, group=group, role='doctor')
    admin = account('demo-admin@example.test')
    grant(admin, demo, 'org_admin')
    for viewer in (account('staff@example.test', is_staff=True), admin):
        response = signed_in(viewer).get(LIST)
        assert not rows(response)[signup.person_id].get('demographics_redacted')
        assert marked(response) == {}
    # A role held at a real organization still marks the record.
    grant(signup.person.portal_user.identity, OrganizationFactory(), 'analyst')
    staff = Identity.objects.get(email='staff@example.test')
    assert marked(signed_in(staff).get(LIST)) == {signup.person_id: ['analyst']}


def test_a_group_grant_is_shown_to_staff_and_not_to_an_org_admin(home, people):
    group = PatientGroup.objects.create(name='Study', organization=home)
    identity = people['plain'].person.portal_user.identity
    GroupAccess.objects.create(identity=identity, group=group, role='doctor')
    staff = account('staff@example.test', is_staff=True)
    assert marked(signed_in(staff).get(LIST))[people['plain'].person_id] == ['doctor']
    admin = account('org-admin@example.test')
    grant(admin, home, 'org_admin')
    assert people['plain'].person_id not in marked(signed_in(admin).get(LIST))


# -- the patient list: who is told nothing ----------------------------------


def test_an_admin_through_a_trust_lists_the_patients_but_is_told_only_their_own_orgs_roles(
        home, elsewhere, people):
    """``home`` trusts ``elsewhere``, so its admin reads home's patients; home's
    access grants stay home's to read (``IsStaffOrAccessAdmin``)."""
    OrgTrust.objects.create(granting_org=home, trusted_org=elsewhere)
    viewer = account('other-admin@example.test')
    grant(viewer, elsewhere, 'org_admin')
    response = signed_in(viewer).get(LIST)
    assert ids(people, 'doctor', 'admin', 'staff') <= set(rows(response))
    assert marked(response) == {people['away'].person_id: ['doctor']}


def test_a_doctor_without_admin_rights_is_told_nothing(home, people):
    viewer = account('doctor@example.test')
    group = PatientGroup.objects.create(name='Panel', organization=home)
    GroupAccess.objects.create(identity=viewer, group=group, role='doctor')
    grant(viewer, home, 'doctor')
    for record in people.values():
        PatientGroupMembership.objects.create(group=group, person_id=record.person_id)
    response = signed_in(viewer).get(LIST)
    assert ids(people, 'doctor', 'admin', 'staff') <= set(rows(response))
    assert marked(response) == {}


def test_an_analyst_and_a_patient_are_told_nothing(home, people):
    analyst = account('analyst@example.test')
    grant(analyst, home, 'analyst')
    assert marked(signed_in(analyst).get(LIST)) == {}
    # A patient who is also a doctor here lists their own record, unmarked.
    own = people['doctor'].person.portal_user.identity
    response = signed_in(own).get(LIST)
    assert people['doctor'].person_id in rows(response)
    assert marked(response) == {}
    plain = people['plain'].person.portal_user.identity
    assert marked(signed_in(plain).get(LIST)) == {}


def test_the_de_identified_demo_listing_carries_no_marker():
    demo = OrganizationFactory(name='Demo', slug='demo', allows_patient_signup=True)
    sample = patient(demo, 'demo-doctor-patient@example.test', roles=['doctor'])
    for role in ('analyst', 'patient'):
        reader = account(f'demo-{role}@example.test')
        grant(reader, demo, role)
        response = signed_in(reader).get(LIST)
        row = rows(response)[sample.person_id]
        assert row['demographics_redacted'] is True
        assert marked(response) == {}, role


def test_a_record_whose_demographics_are_withheld_is_not_marked(home, people):
    record = people['doctor']
    record.suppress_demographics_for_others = True
    record.save(update_fields=['suppress_demographics_for_others'])
    for viewer in (account('staff@example.test', is_staff=True), account('org-admin@example.test')):
        grant(viewer, home, 'org_admin')
        response = signed_in(viewer).get(LIST)
        assert rows(response)[record.person_id]['demographics_redacted'] is True
        found = marked(response)
        assert record.person_id not in found
        assert people['admin'].person_id in found


def test_a_service_token_is_told_nothing(settings, people):
    secret = 'account-roles-service-secret-000000000000'
    settings.SERVICE_AUTH_TOKENS = {'etl': {'token': secret, 'scopes': 'system/*.read patient/*.read'}}
    settings.SERVICE_AUTH_TOKEN = ''
    machine = APIClient()
    machine.credentials(HTTP_AUTHORIZATION=f'Bearer {secret}')
    response = machine.get(LIST)
    assert ids(people, 'doctor', 'admin', 'staff') <= set(rows(response)), 'the token lists patients'
    assert marked(response) == {}


def test_a_credential_that_is_not_a_session_is_told_nothing(people):
    """Staff authority presented without the person at the page."""
    client = APIClient()
    client.force_authenticate(user=account('staff@example.test', is_staff=True))
    response = client.get(LIST)
    assert people['doctor'].person_id in rows(response)
    assert marked(response) == {}


def test_the_legacy_list_and_the_csv_export_carry_no_roles(people):
    viewer = account('staff@example.test', is_staff=True)
    client = signed_in(viewer)
    assert marked(client.get(LIST)), 'the v1 list does'
    assert marked(client.get(LEGACY_LIST)) == {}
    assert marked(client.get(LEGACY_LIST, {'page': 1, 'page_size': 50})) == {}
    export = client.get(f'{LIST}export-csv/')
    assert export.status_code == 200
    assert b'other_roles' not in export.content
    # The detail gains no key. (Its ``user`` block has always carried the
    # linked account as ``UserSerializer`` gives it; that is not this rule's.)
    detail = client.get(f"{LIST}{people['doctor'].person_id}/")
    assert 'other_roles' not in detail.data['patient_info']
    assert 'other_roles' not in detail.data['user']


def test_the_paginated_list_is_marked_too(people):
    viewer = account('staff@example.test', is_staff=True)
    found = marked(signed_in(viewer).get(LIST, {'page': 1, 'page_size': 50}))
    assert found[people['both'].person_id] == ['doctor', 'analyst']
    assert len(found) == 6


@pytest.mark.parametrize('viewer_role', ['staff', 'org_admin'])
def test_the_list_makes_the_same_number_of_queries_for_any_number_of_rows(home, viewer_role):
    viewer = account('viewer@example.test', is_staff=viewer_role == 'staff')
    grant(viewer, home, 'org_admin')
    client = signed_in(viewer)

    def count():
        client.get(LIST)  # the session and its caches, once
        with CaptureQueriesContext(connection) as queries:
            response = client.get(LIST)
        return len(queries), marked(response)

    for index in range(3):
        patient(home, f'small-{index}@example.test', roles=['doctor', 'analyst'])
    small, small_marked = count()
    for index in range(9):
        patient(home, f'large-{index}@example.test', roles=['org_admin'])
    large, large_marked = count()
    assert (len(small_marked), len(large_marked)) == (3, 12)
    assert small == large


# -- the service functions ---------------------------------------------------


def test_other_roles_is_empty_for_a_viewer_who_administers_nothing(home, people):
    doctor = account('doctor@example.test')
    grant(doctor, home, 'doctor')
    everyone = [record.person_id for record in people.values()]
    with CaptureQueriesContext(connection) as queries:
        assert other_roles_by_person(doctor, everyone) == {}
    assert len(queries) == 1
    assert other_roles_by_person(None, everyone) == {}
    assert other_roles_by_person(account('staff@example.test', is_staff=True), []) == {}


def test_who_administers_an_organizations_access(home, elsewhere):
    direct, expired, trusted, grouped = (
        account(f'{name}@example.test') for name in ('direct', 'expired', 'trusted', 'grouped'))
    grant(direct, home, 'org_admin')
    grant(expired, home, 'org_admin', expires_at=timezone.now() - timedelta(days=1))
    grant(trusted, elsewhere, 'org_admin')
    OrgTrust.objects.create(granting_org=home, trusted_org=elsewhere)
    GroupAccess.objects.create(
        identity=grouped, role='org_admin',
        group=PatientGroup.objects.create(name='Unit', organization=home))
    assert set(get_access_admin_orgs(direct)) == {home}
    assert set(get_access_admin_orgs(expired)) == set()
    assert set(get_access_admin_orgs(trusted)) == {elsewhere}
    assert set(get_access_admin_orgs(grouped)) == set()
    assert set(get_access_admin_orgs(account('staff@example.test', is_staff=True))) == {home, elsewhere}
    for identity in (direct, expired, trusted, grouped):
        assert has_explicit_org_admin_access(identity, 'home') == (identity is direct)
    home.is_active = False
    home.save(update_fields=['is_active'])
    assert set(get_access_admin_orgs(direct)) == set()
    assert has_explicit_org_admin_access(direct, 'home') is False


def test_accessible_person_ids_agrees_with_can_access_patient(home, elsewhere, people):
    away_record = patient(elsewhere, 'far@example.test')
    group = PatientGroup.objects.create(name='Panel', organization=home)
    PatientGroupMembership.objects.create(group=group, person_id=people['plain'].person_id)
    everyone = [record.person_id for record in people.values()] + [away_record.person_id]

    group_doctor = account('group-doctor@example.test')
    GroupAccess.objects.create(identity=group_doctor, group=group, role='doctor')
    org_doctor = account('org-doctor@example.test')
    grant(org_doctor, home, 'doctor')
    expired_doctor = account('expired-doctor@example.test')
    grant(expired_doctor, home, 'doctor', expires_at=timezone.now() - timedelta(days=1))
    analyst = account('analyst@example.test')
    grant(analyst, home, 'analyst')
    admin = account('admin@example.test')
    grant(admin, home, 'org_admin')
    trust_admin = account('trust-admin@example.test')
    grant(trust_admin, elsewhere, 'doctor')
    OrgTrust.objects.create(granting_org=home, trusted_org=elsewhere)
    representative = account('representative@example.test')
    PersonalRepresentative.objects.create(
        representative=representative, person_id=people['doctor'].person_id,
        relationship='caregiver', verification_status='VERIFIED')
    PersonalRepresentative.objects.create(
        representative=representative, person_id=people['admin'].person_id,
        relationship='caregiver', verification_status='PENDING')
    actors = {
        'group doctor': group_doctor, 'org doctor': org_doctor,
        'expired doctor': expired_doctor, 'analyst': analyst, 'org admin': admin,
        'trust admin': trust_admin, 'representative': representative,
        'self': people['plain'].person.portal_user.identity,
        'self with a dead link': people['unlinked'].person.portal_user.identity,
        'staff': account('staff@example.test', is_staff=True),
        'nobody': account('nobody@example.test'),
    }
    for name, actor in actors.items():
        expected = {pid for pid in everyone if can_access_patient(actor, pid)}
        assert accessible_person_ids(actor, everyone) == expected, name
    assert accessible_person_ids(actors['group doctor'], everyone) == {people['plain'].person_id}
    assert accessible_person_ids(actors['analyst'], everyone) == set()
    assert accessible_person_ids(None, everyone) == set()
    assert accessible_person_ids(actors['staff'], [None]) == set()

    with CaptureQueriesContext(connection) as few:
        accessible_person_ids(admin, everyone[:2])
    with CaptureQueriesContext(connection) as many:
        accessible_person_ids(admin, everyone)
    assert len(few) == len(many)


# -- the access grants: the Patient role's link ------------------------------


def access(client, slug='home', prefix='/api/v1'):
    response = client.get(f'{prefix}/orgs/{slug}/access/')
    assert response.status_code == 200, response.content[:300]
    return response.data


def links(data):
    return {(row['email'], row['role']): row['person_id'] for row in data}


def test_a_patient_grant_links_to_the_record_the_admin_can_open(home, elsewhere, people):
    far = patient(elsewhere, 'far@example.test')
    grant(far.person.portal_user.identity, home, 'patient')
    no_link = account('no-link@example.test')
    grant(no_link, home, 'patient')
    no_record = account('no-record@example.test')
    grant(no_record, home, 'patient')
    PatientUser.objects.create(identity=no_record, person=PersonFactory())
    viewer = account('org-admin@example.test')
    grant(viewer, home, 'org_admin')

    found = links(access(signed_in(viewer)))
    assert found[('doctor-patient@example.test', 'patient')] == people['doctor'].person_id
    assert found[('plain-patient@example.test', 'patient')] == people['plain'].person_id
    # Only the Patient role links; the same account's other role does not.
    assert found[('doctor-patient@example.test', 'doctor')] is None
    assert found[('org-admin@example.test', 'org_admin')] is None
    # A record at an organization this admin cannot read, a patient with no
    # linked person, a dead link and a person with no record: plain text.
    assert found[('far@example.test', 'patient')] is None
    assert found[('no-link@example.test', 'patient')] is None
    assert found[('unlinked@example.test', 'patient')] is None
    assert found[('no-record@example.test', 'patient')] is None

    staff = links(access(signed_in(account('staff@example.test', is_staff=True))))
    assert staff[('far@example.test', 'patient')] == far.person_id
    assert staff[('no-record@example.test', 'patient')] is None
    assert staff[('doctor-patient@example.test', 'doctor')] is None


def test_an_admin_who_is_a_patient_gets_the_link_to_their_own_record(home):
    own = patient(home, 'self-admin@example.test', roles=['org_admin'])
    viewer = own.person.portal_user.identity
    found = links(access(signed_in(viewer)))
    assert found[('self-admin@example.test', 'patient')] == own.person_id
    assert found[('self-admin@example.test', 'org_admin')] is None


def test_a_record_whose_demographics_are_withheld_is_not_linked_except_to_its_owner(home, people):
    """The grant's row names the account; the list shows that record withheld."""
    own = patient(home, 'self-admin@example.test', roles=['org_admin'])
    for record in (people['plain'], own):
        record.suppress_demographics_for_others = True
        record.save(update_fields=['suppress_demographics_for_others'])
    viewers = {
        'staff': account('staff@example.test', is_staff=True),
        'owner': own.person.portal_user.identity,
    }
    for name, viewer in viewers.items():
        found = links(access(signed_in(viewer)))
        assert found[('plain-patient@example.test', 'patient')] is None, name
        assert found[('doctor-patient@example.test', 'patient')] == people['doctor'].person_id, name
    assert links(access(signed_in(viewers['staff'])))[('self-admin@example.test', 'patient')] is None
    assert links(access(signed_in(viewers['owner'])))[('self-admin@example.test', 'patient')] == own.person_id


def test_the_link_is_for_a_signed_in_admin_on_v1_only(home, people):
    viewer = account('org-admin@example.test')
    grant(viewer, home, 'org_admin')
    assert all('person_id' not in row for row in access(signed_in(viewer), prefix='/api'))
    token = APIClient()
    token.force_authenticate(user=viewer)
    assert set(links(access(token)).values()) == {None}
    for role in ('doctor', 'analyst', 'patient'):
        other = account(f'{role}-reader@example.test')
        grant(other, home, role)
        assert signed_in(other).get('/api/v1/orgs/home/access/').status_code == 403, role
    stranger = account('other-admin@example.test')
    grant(stranger, OrganizationFactory(), 'org_admin')
    assert signed_in(stranger).get('/api/v1/orgs/home/access/').status_code == 403


def test_the_access_grants_make_the_same_number_of_queries_for_any_number_of_patients(home):
    viewer = account('org-admin@example.test')
    grant(viewer, home, 'org_admin')
    client = signed_in(viewer)

    def count():
        access(client)
        with CaptureQueriesContext(connection) as queries:
            data = access(client)
        return len(queries), sum(1 for row in data if row['person_id'])

    for index in range(2):
        patient(home, f'few-{index}@example.test')
    few, few_links = count()
    for index in range(6):
        patient(home, f'more-{index}@example.test', roles=['doctor'])
    more, more_links = count()
    assert (few_links, more_links) == (2, 8)
    assert few == more


def test_record_links_need_a_viewer_and_a_patient_grant(home, people):
    grants = list(GroupAccess.objects.filter(org=home).select_related('identity'))
    assert patient_record_links(None, grants) == {}
    professional = [g for g in grants if g.role != 'patient']
    staff = account('staff@example.test', is_staff=True)
    with CaptureQueriesContext(connection) as queries:
        assert patient_record_links(staff, professional) == {}
    assert len(queries) == 0
