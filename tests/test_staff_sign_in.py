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
        'address_claim': 'preferred_username',
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
    token = f'invitation-token-{email}-{role}'
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
    # Role staff: the admin through the role, never is_staff (full access across the API).
    assert not identity.is_staff and not identity.is_superuser and not identity.has_usable_password()
    assert list(identity.groups.values_list('name', flat=True)) == ['Member support']
    invitation.refresh_from_db()
    assert invitation.accepted_identity == identity and not invitation.is_open
    page = client.get('/admin/')
    assert page.status_code == 200 and b'Alex' in page.content  # greeted by name, not issuer and subject
    assert client.session.get_expiry_age() <= 8 * 3600
    assert client.session[staff_sign_in.PROVIDER_SESSION_KEY] == 'work'


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


def staff_client(*roles, email='staffer@example.test', full=False):
    identity = Identity.objects.create_user(email=email, password=None)
    identity.is_staff = full
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
    _, other = staff_client('Member support', email='other@example.test')
    other.groups.add(Group.objects.create(name='Not a staff role'))

    form = client.get(f'/admin/patient_portal/identity/{other.pk}/change/').content
    assert b'Staff manager' in form and b'Not a staff role' not in form

    manager = Group.objects.get(name='Staff manager')
    client.post(f'/admin/patient_portal/identity/{other.pk}/change/', {'groups': [manager.pk]})
    # the role changed; the group a superuser gave is kept
    assert set(other.groups.values_list('name', flat=True)) == {'Staff manager', 'Not a staff role'}

    client.post('/admin/patient_portal/identity/', {'action': 'remove_staff_access', '_selected_action': [other.pk]})
    assert not other.groups.exists()
    assert Client().get('/admin/').status_code == 302


def test_a_staff_manager_cannot_remove_full_staff_or_change_their_role():
    client, _ = staff_client('Staff manager')
    _, full = staff_client(email='full@example.test', full=True)

    client.post('/admin/patient_portal/identity/', {'action': 'remove_staff_access', '_selected_action': [full.pk]})
    client.post(f'/admin/patient_portal/identity/{full.pk}/change/',
                {'groups': [Group.objects.get(name='Staff manager').pk]})

    full.refresh_from_db()
    assert full.is_staff and not full.groups.exists()


def test_role_staff_get_no_patient_data_from_the_api():
    from omop_core.models import Organization, PatientRecord, Person

    person = Person.objects.create(person_id=990077, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown')
    PatientRecord.objects.create(person=person, organization=Organization.objects.create(name='O', slug='o'))
    for role in ('Member support', 'Staff manager'):
        client, _ = staff_client(role, email=f'{role.split()[0].lower()}@example.test')
        for path in ('/api/patient-info/', '/api/v1/patient-records/', f'/api/patient-info/{person.person_id}/'):
            response = client.get(path)
            assert response.status_code in (200, 403, 404), (role, path, response.status_code)
            assert b'990077' not in response.content, (role, path)
    full, _ = staff_client(email='full@example.test', full=True)
    assert b'990077' in full.get('/api/patient-info/').content  # full staff keep full access


def test_member_support_acts_on_members_and_never_on_staff():
    client, _ = staff_client('Member support')
    member = Identity.objects.create_user(email='member@example.test', password='Member-passw0rd-long')
    _, other_staff = staff_client('Staff manager', email='other@example.test')

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


def test_only_a_superuser_invites_full_staff():
    manager, _ = staff_client('Staff manager', email='manager@example.test')
    manager.post('/admin/patient_portal/staffinvitation/add/', {
        'email': 'x@flf.example', 'provider': 'work', 'role': '__full_staff__'})
    assert not StaffInvitation.objects.exists()

    root = Identity.objects.create_superuser(email='root@example.test', password='Root-passw0rd-long')
    client = Client()
    client.force_login(root, backend='patient_portal.backends.EmailBackend')
    client.post('/admin/patient_portal/staffinvitation/add/', {
        'email': 'ops@example.test', 'provider': 'work', 'role': '__full_staff__'})
    invitation = StaffInvitation.objects.get()
    assert invitation.full_staff and invitation.role == ''


def test_an_invitation_restores_removed_staff_and_changes_a_role(fake):
    _, token = invite(role='Member support')
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    identity = Identity.objects.get(sub='sub-1')
    identity.groups.clear()  # access removed
    assert sign_in(Client(), fake, '/admin/sign-in/work/')[0].status_code == 403

    _, token = invite(role='Staff manager')
    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    assert response.status_code == 302
    assert list(identity.groups.values_list('name', flat=True)) == ['Staff manager']
    assert Identity.objects.filter(sub='sub-1').count() == 1


@pytest.mark.parametrize('claims', [
    {'email': 'alex@flf.example', 'email_verified': False},
    {'email': 'alex@flf.example', 'email_verified': 'false'},
    {'email': 'alex@flf.example'},  # not said to be verified
])
def test_an_email_address_counts_only_when_the_provider_says_it_is_verified(settings, fake, claims):
    settings.STAFF_SIGN_IN_PROVIDERS[0]['address_claim'] = 'email'
    _, token = invite()
    fake.claims = claims

    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    assert response.status_code == 403 and not Identity.objects.filter(sub='sub-1').exists()


def test_password_sessions_of_staff_end_when_staff_passwords_are_switched_off(settings, fake):
    _, token = invite()
    by_provider = Client()
    sign_in(by_provider, fake, f'/admin/sign-in/invitation/{token}/')
    by_password, _ = staff_client(email='old@example.test', full=True)
    member = Identity.objects.create_user(email='member@example.test', password='Member-passw0rd-long')
    member_client = Client()
    member_client.force_login(member, backend='patient_portal.backends.EmailBackend')

    settings.STAFF_PASSWORD_SIGN_IN = False

    assert by_password.get('/admin/').status_code == 302  # signed out
    assert '_auth_user_id' not in by_password.session
    assert by_provider.get('/admin/').status_code == 200
    member_client.get('/api/v1/user/')
    assert member_client.session.get('_auth_user_id') == str(member.pk)


def test_member_support_never_sees_a_members_invitation_token():
    from omop_core.models import Person
    from patient_portal.models import PatientInvitation

    person = Person.objects.create(person_id=990002, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown')
    invitation = PatientInvitation.objects.create(
        person=person, email='m@example.test', token='a' * 64, expires_at=timezone.now() + timezone.timedelta(days=1))
    client, _ = staff_client('Member support')

    page = client.get(f'/admin/patient_portal/patientinvitation/{invitation.pk}/change/')

    assert page.status_code == 200 and b'a' * 64 not in page.content


def test_sending_an_invitation_again_only_resends_the_latest_to_the_records_address():
    from omop_core.models import Organization, PatientRecord, Person
    from patient_portal.models import PatientInvitation

    person = Person.objects.create(person_id=990003, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown',
                                   email='new@example.test')
    PatientRecord.objects.create(person=person, organization=Organization.objects.create(name='O', slug='o'))
    soon = timezone.now() + timezone.timedelta(days=1)
    old = PatientInvitation.objects.create(person=person, email='old@example.test', token='b' * 64, expires_at=soon,
                                           cancelled_at=timezone.now())
    current = PatientInvitation.objects.create(person=person, email='new@example.test', token='c' * 64,
                                               expires_at=soon)
    client, _ = staff_client('Member support')

    client.post('/admin/patient_portal/patientinvitation/', {'action': 'resend', '_selected_action': [old.pk]})
    assert len(mail.outbox) == 0
    client.post('/admin/patient_portal/patientinvitation/', {'action': 'resend', '_selected_action': [current.pk]})

    assert [m.to for m in mail.outbox] == [['new@example.test']]
    current.refresh_from_db()
    person.refresh_from_db()
    assert current.token != 'c' * 64 and person.email == 'new@example.test'


def test_unreachable_provider_keys_or_an_odd_state_are_refusals_not_errors(fake, monkeypatch):
    _, token = invite()

    def unreachable(self, token):
        raise jwt.PyJWKClientConnectionError('down')

    monkeypatch.setattr(jwt.PyJWKClient, 'get_signing_key_from_jwt', unreachable)
    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    assert response.status_code == 403 and b'could not be reached' in response.content

    client = Client()
    client.get(f'/admin/sign-in/invitation/{token}/')
    assert client.get('/admin/sign-in/callback/', {'state': 'é', 'code': 'c'}).status_code == 400


def test_a_sign_in_starts_on_the_host_the_provider_returns_to(settings):
    settings.ALLOWED_HOSTS = ['*']
    response = Client(HTTP_HOST='another-name.example').get('/admin/sign-in/work/?next=/admin/')
    assert response.status_code == 302 and response['Location'] == 'http://testserver/admin/sign-in/work/?next=/admin/'


def test_invitation_links_are_redacted_from_logs_and_the_audit_trail():
    from promop.sentry import redact_secret_segments

    assert redact_secret_segments('/admin/sign-in/invitation/abc123/') == '/admin/sign-in/invitation/:secret/'


def test_a_staff_member_invited_as_a_member_gets_a_member_account_of_their_own(fake):
    from patient_portal.api.patient_invitations import _find_local_or_any_identity

    _, token = invite()
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    assert _find_local_or_any_identity('alex@flf.example') is None


def test_the_settings_are_checked(settings):
    from patient_portal.checks import staff_sign_in_check

    settings.STAFF_SIGN_IN_PROVIDERS = [{'key': 'a', 'label': 'A', 'issuer': 'http://x', 'client_id': 'c',
                                         'client_secret_env': 'S'}, {'key': 'a'}]
    ids = {e.id for e in staff_sign_in_check(None)}
    assert {'patient_portal.E010', 'patient_portal.E012', 'patient_portal.W010'} <= ids

    settings.STAFF_SIGN_IN_PROVIDERS = []
    settings.STAFF_PASSWORD_SIGN_IN = False
    assert {e.id for e in staff_sign_in_check(None)} == {'patient_portal.E013'}
