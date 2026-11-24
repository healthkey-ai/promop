"""Staff sign-in through an OpenID Connect provider, staff invitations, and the
limits on what staff roles can do in the admin (docs/staff-sign-in.md).

The provider is faked at its edges: its discovery document, its token
endpoint, and its signing key (an RSA key made here), so every check on the
ID token is the real one.
"""
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.test import Client
from django.utils import timezone

from patient_portal import staff_sign_in
from patient_portal.backends import EmailBackend
from patient_portal.models import ConsentEvent, ConsentPurpose, ConsentText, Identity, PatientUser, StaffInvitation

pytestmark = pytest.mark.django_db

ISSUER = 'https://login.example.test/tenant-1/v2.0'
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CONFIG = {
    'issuer': ISSUER,
    'authorization_endpoint': 'https://login.example.test/authorize',
    'token_endpoint': 'https://login.example.test/token',
    'jwks_uri': 'https://login.example.test/keys',
}


@pytest.fixture(autouse=True)
def provider(settings, monkeypatch):
    settings.STAFF_SIGN_IN_PROVIDERS = [{
        'key': 'work', 'label': 'Work account', 'issuer': ISSUER, 'client_id': 'client-1',
        'client_secret_env': 'TEST_STAFF_SECRET', 'claims': {'tid': 'tenant-1'}, 'require_mfa': True,
    }]
    settings.STAFF_ROLES = ['Member support', 'Staff manager']
    settings.APP_BASE_URL = 'http://testserver'
    monkeypatch.setenv('TEST_STAFF_SECRET', 'secret-1')
    monkeypatch.setattr(staff_sign_in, 'discovery', lambda p: CONFIG)
    monkeypatch.setattr(jwt.PyJWKClient, 'get_signing_key_from_jwt',
                        lambda self, token: SimpleNamespace(key=KEY.public_key()))
    support = Group.objects.create(name='Member support')
    support.permissions.add(*Permission.objects.filter(codename__in=[
        'view_identity', 'support_member_accounts', 'view_patientinvitation']))
    manager = Group.objects.create(name='Staff manager')
    manager.permissions.add(*Permission.objects.filter(codename__in=[
        'view_identity', 'change_identity', 'manage_staff']))


class Provider:
    """What the fake provider will put in the next ID token."""

    def __init__(self, monkeypatch):
        self.claims = {}
        self.posted = []
        monkeypatch.setattr(staff_sign_in.requests, 'post', self.token_endpoint)

    def token_endpoint(self, url, timeout, data):
        self.posted.append(data)
        now = int(time.time())
        claims = {'iss': ISSUER, 'aud': 'client-1', 'sub': 'sub-1', 'iat': now, 'exp': now + 300,
                  'tid': 'tenant-1', 'amr': ['pwd', 'mfa'], 'preferred_username': 'alex@flf.example',
                  'name': 'Alex Staff', 'nonce': self.nonce, **self.claims}
        claims = {k: v for k, v in claims.items() if v is not None}
        token = jwt.encode(claims, KEY, algorithm='RS256')
        return SimpleNamespace(status_code=200, json=lambda: {'id_token': token})


@pytest.fixture
def fake(monkeypatch):
    return Provider(monkeypatch)


def invite(email='alex@flf.example', role='Member support'):
    token = 'invitation-token-' + email
    invitation = StaffInvitation.objects.create(
        email=email, provider='work', role=role, token_hash=staff_sign_in.hash_token(token),
        expires_at=timezone.now() + timezone.timedelta(days=7),
    )
    return invitation, token


def sign_in(client, fake, path, **callback):
    """Follow a sign-in: to the provider, and back with a code."""
    started = client.get(path)
    assert started.status_code == 302, started.content
    query = parse_qs(urlparse(started['Location']).query)
    fake.nonce = query['nonce'][0]
    params = {'state': query['state'][0], 'code': 'code-1', **callback}
    return client.get('/admin/sign-in/callback/', params), query


def test_an_invitation_makes_a_staff_account_with_its_role_and_no_password(fake):
    invitation, token = invite()
    client = Client()

    response, query = sign_in(client, fake, f'/admin/sign-in/invitation/{token}/')

    assert response.status_code == 302 and response['Location'] == '/admin/'
    assert query['code_challenge_method'] == ['S256'] and query['client_id'] == ['client-1']
    assert fake.posted[0]['code_verifier'] and fake.posted[0]['client_secret'] == 'secret-1'
    identity = Identity.objects.get(issuer=ISSUER, sub='sub-1')
    assert identity.is_staff and not identity.is_superuser and not identity.has_usable_password()
    assert list(identity.groups.values_list('name', flat=True)) == ['Member support']
    invitation.refresh_from_db()
    assert invitation.accepted_identity == identity and not invitation.is_open
    assert client.get('/admin/').status_code == 200


def test_the_same_account_signs_in_again_without_an_invitation(fake):
    _, token = invite()
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    response, _ = sign_in(Client(), fake, '/admin/sign-in/work/?next=/admin/patient_portal/identity/')

    assert response['Location'] == '/admin/patient_portal/identity/'


def test_an_invitation_works_once_and_only_for_its_address(fake):
    _, token = invite()
    fake.claims = {'preferred_username': 'someone.else@flf.example'}
    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    assert response.status_code == 403 and b'alex@flf.example' in response.content
    assert not Identity.objects.filter(sub='sub-1').exists()

    fake.claims = {}
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    assert Client().get(f'/admin/sign-in/invitation/{token}/').status_code == 404


def test_no_account_without_an_invitation(fake):
    response, _ = sign_in(Client(), fake, '/admin/sign-in/work/')

    assert response.status_code == 403 and b'invitation' in response.content
    assert not Identity.objects.filter(sub='sub-1').exists()


@pytest.mark.parametrize('claims, refused_for', [
    ({'amr': ['pwd']}, b'two-step verification'),
    ({'amr': None}, b'two-step verification'),
    ({'tid': 'another-tenant'}, b'organisation'),
    ({'aud': 'another-client'}, b'could not be completed'),
    ({'iss': 'https://elsewhere.example.test'}, b'could not be completed'),
    ({'exp': int(time.time()) - 3600}, b'could not be completed'),
])
def test_a_token_that_fails_a_check_signs_nobody_in(fake, claims, refused_for):
    _, token = invite()
    fake.claims = claims
    client = Client()

    response, _ = sign_in(client, fake, f'/admin/sign-in/invitation/{token}/')

    assert response.status_code == 403 and refused_for in response.content
    assert not Identity.objects.filter(sub='sub-1').exists()
    assert client.get('/admin/').status_code == 302  # still signed out


def test_a_forged_state_or_a_replayed_nonce_is_refused(fake):
    _, token = invite()
    client = Client()
    started = client.get(f'/admin/sign-in/invitation/{token}/')
    fake.nonce = parse_qs(urlparse(started['Location']).query)['nonce'][0]

    assert client.get('/admin/sign-in/callback/', {'state': 'forged', 'code': 'c'}).status_code == 400

    started = client.get(f'/admin/sign-in/invitation/{token}/')
    fake.nonce = 'a-nonce-from-another-sign-in'
    state = parse_qs(urlparse(started['Location']).query)['state'][0]
    assert client.get('/admin/sign-in/callback/', {'state': state, 'code': 'c'}).status_code == 403


def test_a_next_address_elsewhere_is_ignored(fake):
    _, token = invite()
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    response, _ = sign_in(Client(), fake, '/admin/sign-in/work/?next=https://evil.example/')

    assert response['Location'] == '/admin/'


def test_staff_passwords_can_be_turned_off_without_touching_members(settings):
    staff = Identity.objects.create_user(email='staff@example.test', password='Staff-passw0rd-long')
    staff.is_staff = True
    staff.save()
    member = Identity.objects.create_user(email='member@example.test', password='Member-passw0rd-long')
    backend = EmailBackend()

    settings.STAFF_PASSWORD_SIGN_IN = False

    assert backend.authenticate(None, username='staff@example.test', password='Staff-passw0rd-long') is None
    assert backend.authenticate(None, username='staff@example.test', password='wrong') is None
    staff.refresh_from_db()
    assert staff.failed_login_count == 0  # nobody can lock a staff account out
    assert backend.authenticate(None, username='member@example.test', password='Member-passw0rd-long') == member


def test_the_sign_in_page_offers_the_provider_and_hides_passwords_when_they_are_off(settings):
    assert b'Sign in with Work account' in Client().get('/admin/login/').content
    assert b'id_password' in Client().get('/admin/login/').content

    settings.STAFF_PASSWORD_SIGN_IN = False

    page = Client().get('/admin/login/?next=/admin/').content
    assert b'Sign in with Work account' in page and b'id_password' not in page


def staff_client(*roles, email='staffer@example.test'):
    identity = Identity.objects.create_user(email=email, password=None)
    identity.is_staff = True
    identity.save()
    identity.groups.add(*Group.objects.filter(name__in=roles))
    client = Client()
    client.force_login(identity, backend='patient_portal.backends.EmailBackend')
    return client, identity


def test_no_staff_role_can_make_itself_superuser_or_set_a_password():
    client, me = staff_client('Staff manager')
    member = Identity.objects.create_user(email='member@example.test', password='Member-passw0rd-long')

    client.post(f'/admin/patient_portal/identity/{me.pk}/change/',
                {'is_superuser': 'on', 'is_staff': 'on', 'email': 'me@example.test', 'groups': []})
    me.refresh_from_db()
    assert not me.is_superuser and me.email == 'staffer@example.test'
    assert client.get(f'/admin/patient_portal/identity/{member.pk}/password/').status_code == 403


def test_a_staff_manager_sets_another_staff_members_role_from_the_staff_roles_only():
    client, _ = staff_client('Staff manager')
    _, other = staff_client(email='other@example.test')
    Group.objects.create(name='Not a staff role')

    form = client.get(f'/admin/patient_portal/identity/{other.pk}/change/').content
    assert b'Member support' in form and b'Not a staff role' not in form

    support = Group.objects.get(name='Member support')
    client.post(f'/admin/patient_portal/identity/{other.pk}/change/', {'groups': [support.pk]})
    assert list(other.groups.values_list('name', flat=True)) == ['Member support']

    client.post('/admin/patient_portal/identity/', {'action': 'remove_staff_access', '_selected_action': [other.pk]})
    other.refresh_from_db()
    assert not other.is_staff and not other.groups.exists()


def test_member_support_acts_on_members_and_never_on_staff():
    client, _ = staff_client('Member support')
    member = Identity.objects.create_user(email='member@example.test', password='Member-passw0rd-long')
    _, other_staff = staff_client(email='other@example.test')

    client.post('/admin/patient_portal/identity/', {
        'action': 'require_password_change', '_selected_action': [member.pk, other_staff.pk]})

    member.refresh_from_db()
    other_staff.refresh_from_db()
    assert member.must_change_password and not other_staff.must_change_password
    page = client.get('/admin/patient_portal/identity/').content
    assert b'grant_premium' not in page and b'remove_staff_access' not in page
    assert client.get('/admin/omop_core/patientrecord/').status_code == 403  # no health data


def test_member_support_withdraws_a_permission_for_a_member(monkeypatch):
    from omop_core.models import Person

    client, me = staff_client('Member support')
    member = Identity.objects.create_user(email='member@example.test', password='Member-passw0rd-long')
    person = Person.objects.create(person_id=990001, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown')
    PatientUser.objects.create(identity=member, person=person)
    purpose = ConsentPurpose.objects.create(key='updates')
    ConsentText.objects.create(purpose=purpose, version='1', title='Updates from us', body='...',
                               published_at=timezone.now() - timezone.timedelta(days=1))
    ConsentEvent.objects.create(person=person, purpose=purpose, version='1', decision=ConsentEvent.GRANTED)

    asked = client.post('/admin/patient_portal/identity/', {
        'action': 'withdraw_permission', '_selected_action': [member.pk]})
    assert asked.status_code == 200 and b'Updates from us' in asked.content

    client.post('/admin/patient_portal/identity/', {
        'action': 'withdraw_permission', '_selected_action': [member.pk], 'confirm': 'withdraw',
        'purpose': 'updates'})
    latest = ConsentEvent.objects.filter(person=person).order_by('-recorded_at', '-id').first()
    assert (latest.decision, latest.source, latest.recorded_by) == (ConsentEvent.WITHDRAWN, ConsentEvent.STAFF, me)


def test_inviting_staff_needs_the_manager_permission_and_keeps_only_the_tokens_hash():
    support, _ = staff_client('Member support', email='support@example.test')
    assert support.get('/admin/patient_portal/staffinvitation/add/').status_code == 403

    manager, me = staff_client('Staff manager', email='manager@example.test')
    response = manager.post('/admin/patient_portal/staffinvitation/add/', {
        'email': 'New.Person@flf.example', 'provider': 'work', 'role': 'Member support'}, follow=True)

    invitation = StaffInvitation.objects.get()
    assert (invitation.email, invitation.role, invitation.invited_by) == ('new.person@flf.example', 'Member support', me)
    link = mail.outbox[0].body.split('\n')[3]
    assert link.startswith('http://testserver/admin/sign-in/invitation/')
    assert invitation.token_hash == staff_sign_in.hash_token(link.rstrip('/').rsplit('/', 1)[1])
    assert link.encode() in response.content  # shown once to the inviter too

    refused = manager.post('/admin/patient_portal/staffinvitation/add/', {
        'email': 'x@flf.example', 'provider': 'work', 'role': 'superuser'})
    assert refused.status_code == 200 and StaffInvitation.objects.count() == 1  # not a staff role
