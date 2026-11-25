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

REAL_DISCOVERY = staff_sign_in.discovery  # the fixture below replaces it

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
    settings.STAFF_ORGANIZATIONS = ['flf-test']
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


def test_no_account_without_an_invitation(fake, monkeypatch):
    from django.contrib import admin

    monkeypatch.setattr(admin.site, 'site_header', 'Example back office')
    response, _ = sign_in(Client(), fake, '/admin/sign-in/work/')

    assert response.status_code == 403 and b'invitation' in response.content
    assert b'Example back office' in response.content  # the deployment's header, not Django's
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


def provider_session(client, provider='work'):
    """Mark a test client's session as a provider sign-in, as the callback does."""
    session = client.session
    session[staff_sign_in.PROVIDER_SESSION_KEY] = provider
    session[staff_sign_in.STAFF_UNTIL_KEY] = time.time() + 3600
    session.save()


def make_member(email, person_id, org='flf-test'):
    """A member: a sign-in, a person, their record in ``org`` and the link."""
    from omop_core.models import Organization, PatientRecord, Person

    member = Identity.objects.create_user(email=email, password='Member-passw0rd-long')
    person = Person.objects.create(person_id=person_id, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown')
    organization, _ = Organization.objects.get_or_create(slug=org, defaults={'name': org})
    PatientRecord.objects.create(person=person, organization=organization)
    PatientUser.objects.create(identity=member, person=person)
    return member, person


def staff_client(*roles, email='staffer@example.test', full=False):
    identity = Identity.objects.create_user(email=email, password=None)
    identity.is_staff = full
    identity.save()
    identity.groups.add(*Group.objects.filter(name__in=roles))
    client = Client()
    client.force_login(identity, backend='patient_portal.backends.EmailBackend')
    provider_session(client)
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
    # the staff role goes; the group a superuser gave stays
    assert list(other.groups.values_list('name', flat=True)) == ['Not a staff role']


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
    member, _ = make_member('member@example.test', 990030)
    elsewhere, _ = make_member('elsewhere@example.test', 990031, org='another-organisation')
    _, other_staff = staff_client('Staff manager', email='other@example.test')

    client.post('/admin/patient_portal/identity/', {
        'action': 'require_password_change', '_selected_action': [member.pk, other_staff.pk, elsewhere.pk]})
    elsewhere.refresh_from_db()
    assert not elsewhere.must_change_password  # another organisation's member

    member.refresh_from_db()
    other_staff.refresh_from_db()
    assert member.must_change_password and not other_staff.must_change_password
    page = client.get('/admin/patient_portal/identity/').content
    assert b'grant_premium' not in page and b'remove_staff_access' not in page
    assert client.get('/admin/omop_core/patientrecord/').status_code == 403  # no health data


def test_member_support_withdraws_a_permission_for_a_member(monkeypatch):
    from omop_core.models import Person

    client, me = staff_client('Member support')
    member, person = make_member('member@example.test', 990001)
    purpose = ConsentPurpose.objects.create(key='updates')
    ConsentText.objects.create(purpose=purpose, version='1', title='Updates from us', body='...',
                               published_at=timezone.now() - timezone.timedelta(days=1))
    ConsentEvent.objects.create(person=person, purpose=purpose, version='1', decision=ConsentEvent.GRANTED,
                                recorded_at=timezone.now() - timezone.timedelta(days=10))

    asked = client.post('/admin/patient_portal/identity/', {
        'action': 'withdraw_permission', '_selected_action': [member.pk]})
    assert asked.status_code == 200 and b'Updates from us' in asked.content

    asked = timezone.localdate() - timezone.timedelta(days=2)
    tomorrow = timezone.localdate() + timezone.timedelta(days=1)
    for missing_or_future in ('', tomorrow.isoformat()):
        page = client.post('/admin/patient_portal/identity/', {
            'action': 'withdraw_permission', '_selected_action': [member.pk], 'confirm': 'withdraw',
            'purpose': 'updates', 'requested_on': missing_or_future})
        assert b'today or earlier' in page.content
    assert ConsentEvent.objects.filter(person=person).count() == 1  # nothing recorded yet

    client.post('/admin/patient_portal/identity/', {
        'action': 'withdraw_permission', '_selected_action': [member.pk], 'confirm': 'withdraw',
        'purpose': 'updates', 'requested_on': asked.isoformat(), 'note': 'By email to the support address'})
    latest = ConsentEvent.objects.filter(person=person).order_by('-recorded_at', '-id').first()
    assert (latest.decision, latest.source, latest.recorded_by) == (ConsentEvent.WITHDRAWN, ConsentEvent.STAFF, me)
    assert (latest.requested_on, latest.note, latest.version) == (asked, 'By email to the support address', '1')
    from patient_portal.models import ConsentLedgerEntry

    entry = ConsentLedgerEntry.objects.filter(key='updates').order_by('-recorded_at', '-id').first()
    assert (entry.decision, entry.requested_on) == (ConsentEvent.WITHDRAWN, asked)


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
    old = Identity.objects.create_user(email='old@example.test', password=None)
    old.is_staff = True
    old.save()
    by_password = Client()
    by_password.force_login(old, backend='patient_portal.backends.EmailBackend')  # no provider marker
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
    PatientRecord.objects.create(person=person, organization=Organization.objects.create(name='F', slug='flf-test'))
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
    assert response.status_code == 302 and response['Location'] == 'http://testserver/admin/sign-in/work/?next=%2Fadmin%2F'


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


def superuser_client():
    root = Identity.objects.create_superuser(email='root@example.test', password='Root-passw0rd-long')
    client = Client()
    client.force_login(root, backend='patient_portal.backends.EmailBackend')
    return client, root


def test_a_local_staff_account_invited_as_a_member_is_refused_as_before():
    from omop_core.models import Person
    from patient_portal.models import PatientInvitation

    staff = Identity.objects.create_user(email='both@example.test', password='Staff-passw0rd-long')
    staff.is_staff = True
    staff.save()
    person = Person.objects.create(person_id=990010, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown')
    invitation = PatientInvitation.objects.create(person=person, email='both@example.test', token='d' * 64,
                                                  expires_at=timezone.now() + timezone.timedelta(days=1))

    response = Client().post('/api/v1/patient-invitations/accept/',
                             {'token': invitation.token, 'password': 'Member-passw0rd-long'},
                             content_type='application/json')

    assert response.status_code == 400 and 'already exists' in response.json()['error']
    assert Identity.objects.filter(email__iexact='both@example.test', issuer='urn:local').count() == 1


def test_two_local_accounts_on_one_address_sign_neither_in_rather_than_fail():
    for _ in range(2):
        Identity.objects.create_user(email='twice@example.test', password='Twice-passw0rd-long')

    assert EmailBackend().authenticate(None, username='twice@example.test', password='Twice-passw0rd-long') is None


def test_no_role_reaches_groups_or_adds_or_deletes_accounts():
    manager = Group.objects.get(name='Staff manager')
    manager.permissions.add(*Permission.objects.filter(
        codename__in=['change_group', 'view_group', 'add_identity', 'delete_identity']))
    client, _ = staff_client('Staff manager')
    _, root = superuser_client()

    assert client.get('/admin/auth/group/').status_code == 403
    assert client.get(f'/admin/auth/group/{manager.pk}/change/').status_code == 403
    assert client.get('/admin/patient_portal/identity/add/').status_code == 403
    assert client.get(f'/admin/patient_portal/identity/{root.pk}/delete/').status_code == 403
    client.post('/admin/patient_portal/identity/', {'action': 'delete_selected', '_selected_action': [root.pk],
                                                    'post': 'yes'})
    assert Identity.objects.filter(pk=root.pk).exists()


def test_nobody_changes_their_own_role_by_inviting_themselves(fake):
    _, token = invite(role='Staff manager')
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    me = Identity.objects.get(sub='sub-1')
    client = Client()
    client.force_login(me, backend='patient_portal.backends.EmailBackend')
    provider_session(client)

    client.post('/admin/patient_portal/staffinvitation/add/', {
        'email': 'alex@flf.example', 'provider': 'work', 'role': 'Member support'})
    assert not StaffInvitation.objects.filter(invited_by=me).exists()  # refused at the form

    sneaked, token = invite(role='Member support')
    sneaked.invited_by = me
    sneaked.save()
    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    assert response.status_code == 403
    assert list(me.groups.values_list('name', flat=True)) == ['Staff manager']


def test_role_staff_get_no_admin_from_a_password_sign_in():
    identity = Identity.objects.create_user(email='role@example.test', password='Role-passw0rd-long')
    identity.groups.add(Group.objects.get(name='Member support'))
    client = Client()
    client.force_login(identity, backend='patient_portal.backends.EmailBackend')

    assert client.get('/admin/').status_code == 302
    provider_session(client)
    assert client.get('/admin/').status_code == 200


def test_a_provider_sign_in_ends_at_its_deadline():
    client, _ = staff_client('Member support')
    assert client.get('/admin/').status_code == 200

    session = client.session
    session[staff_sign_in.STAFF_UNTIL_KEY] = time.time() - 1
    session.save()

    assert client.get('/admin/').status_code == 302
    assert '_auth_user_id' not in client.session


def test_member_support_only_views_patient_invitations_and_resends_one_at_a_time():
    from omop_core.models import Person
    from patient_portal.models import PatientInvitation

    person = Person.objects.create(person_id=990011, year_of_birth=1970, gender_source_value='female',
                                   race_source_value='unknown', ethnicity_source_value='unknown',
                                   email='m@example.test')
    soon = timezone.now() + timezone.timedelta(days=1)
    other = Person.objects.create(person_id=990012, year_of_birth=1970, gender_source_value='female',
                                  race_source_value='unknown', ethnicity_source_value='unknown', email='n@example.test')
    first = PatientInvitation.objects.create(person=person, email='m@example.test', token='e' * 64, expires_at=soon)
    second = PatientInvitation.objects.create(person=other, email='n@example.test', token='f' * 64, expires_at=soon)
    client, me = staff_client('Member support')

    client.post(f'/admin/patient_portal/patientinvitation/{first.pk}/change/', {'email': me.email})
    first.refresh_from_db()
    assert first.email == 'm@example.test'

    client.post('/admin/patient_portal/patientinvitation/', {
        'action': 'resend', '_selected_action': [first.pk, second.pk]})
    assert len(mail.outbox) == 0


def test_support_actions_are_in_the_admin_history():
    from django.contrib.admin.models import LogEntry

    client, me = staff_client('Member support')
    member, _ = make_member('member@example.test', 990032)

    client.post('/admin/patient_portal/identity/', {
        'action': 'require_password_change', '_selected_action': [member.pk]})
    client.post('/admin/patient_portal/identity/', {'action': 'reset_password', '_selected_action': [member.pk]})

    messages = set(LogEntry.objects.filter(user=me, object_id=str(member.pk)).values_list('change_message', flat=True))
    assert messages == {'Required a password change at next login', 'Emailed a password reset link'}


def test_a_superuser_removes_full_staff_and_cancels_an_invitation():
    client, _ = superuser_client()
    _, full = staff_client(email='full@example.test', full=True)
    invitation, _ = invite()

    client.post('/admin/patient_portal/identity/', {'action': 'remove_staff_access', '_selected_action': [full.pk]})
    client.post('/admin/patient_portal/staffinvitation/', {'action': 'cancel', '_selected_action': [invitation.pk]})

    full.refresh_from_db()
    invitation.refresh_from_db()
    assert not full.is_staff and invitation.cancelled_at is not None and not invitation.is_open


def test_a_cancelled_sign_in_and_an_unreachable_provider_are_refusals(fake, monkeypatch):
    from patient_portal.models import AuditEvent

    client = Client()
    started = client.get('/admin/sign-in/work/')
    state = parse_qs(urlparse(started['Location']).query)['state'][0]
    response = client.get('/admin/sign-in/callback/', {'state': state, 'error': 'access_denied'})
    assert response.status_code == 400
    assert AuditEvent.objects.filter(detail__staff_sign_in='cancelled').exists()

    def unreachable(provider):
        raise staff_sign_in.requests.ConnectionError('down')

    monkeypatch.setattr(staff_sign_in, 'discovery', unreachable)
    assert Client().get('/admin/sign-in/work/').status_code == 502


def test_discovery_must_name_its_issuer_and_https_endpoints(monkeypatch):
    from django.core.cache import cache

    provider = staff_sign_in.providers()['work']

    def answer(config, status=200):
        def get(url, timeout):
            return SimpleNamespace(status_code=status, json=lambda: config,
                                   raise_for_status=lambda: None if status == 200 else (_ for _ in ()).throw(
                                       staff_sign_in.requests.HTTPError(str(status))))
        monkeypatch.setattr(staff_sign_in.requests, 'get', get)
        cache.clear()

    answer(CONFIG)
    assert REAL_DISCOVERY(provider)['token_endpoint'] == CONFIG['token_endpoint']
    for bad in ({**CONFIG, 'issuer': 'https://elsewhere.example.test'}, {**CONFIG, 'jwks_uri': 'http://keys.example.test'}):
        answer(bad)
        with pytest.raises(staff_sign_in.SignInRefused):
            REAL_DISCOVERY(provider)
    answer(CONFIG, status=500)
    with pytest.raises(staff_sign_in.requests.HTTPError):
        REAL_DISCOVERY(provider)


@pytest.mark.parametrize('claims', [
    {'aud': ['client-1', 'another-client']},  # several audiences and no azp
    {'azp': 'another-client'},
])
def test_a_token_for_another_party_is_refused(fake, claims):
    _, token = invite()
    fake.claims = claims

    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    assert response.status_code == 403 and not Identity.objects.filter(sub='sub-1').exists()


def test_a_verified_email_matches_an_invitation(settings, fake):
    settings.STAFF_SIGN_IN_PROVIDERS[0]['address_claim'] = 'email'
    _, token = invite()
    fake.claims = {'email': 'Alex@FLF.example', 'email_verified': True, 'preferred_username': None}

    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')

    assert response.status_code == 302 and Identity.objects.filter(sub='sub-1').exists()


def test_a_switched_off_account_and_a_concurrent_first_sign_in_are_refusals(fake, monkeypatch):
    _, token = invite()
    sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    identity = Identity.objects.get(sub='sub-1')
    identity.is_active = False
    identity.save()
    _, token = invite(role='Staff manager')
    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    assert response.status_code == 403 and b'switched off' in response.content

    from django.db import IntegrityError

    _, token = invite(email='new@flf.example')
    fake.claims = {'sub': 'sub-2', 'preferred_username': 'new@flf.example'}
    real_save = Identity.save

    def save(self, *args, **kwargs):
        if self.sub == 'sub-2' and self.pk is None:
            raise IntegrityError('uq_identity_issuer_sub')
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(Identity, 'save', save)
    response, _ = sign_in(Client(), fake, f'/admin/sign-in/invitation/{token}/')
    assert response.status_code == 403 and b'could not be completed' in response.content


def test_settings_that_could_not_work_are_stopped(settings, monkeypatch):
    from patient_portal.checks import staff_sign_in_check

    monkeypatch.delenv('TEST_STAFF_SECRET')
    settings.APP_BASE_URL = 'http://movement.example'
    ids = {e.id for e in staff_sign_in_check(None)}
    assert {'patient_portal.E014', 'patient_portal.E015'} <= ids

    settings.STAFF_SIGN_IN_PROVIDERS = ['not an object']
    assert 'patient_portal.E016' in {e.id for e in staff_sign_in_check(None)}


def test_closing_a_members_account_needs_its_permission_and_the_typed_address():
    from django.contrib.admin.models import LogEntry
    from omop_core.models import Organization, PatientRecord, Person

    from patient_portal.models import PatientInvitation

    member, person = make_member('leaving@example.test', 990020)
    invitation = PatientInvitation.objects.create(person=person, email='leaving@example.test', token='g' * 64,
                                                  expires_at=timezone.now() + timezone.timedelta(days=1),
                                                  accepted_at=timezone.now())
    _, other_staff = staff_client('Staff manager', email='other@example.test')

    support, _ = staff_client('Member support')
    support.post('/admin/patient_portal/identity/', {
        'action': 'require_password_change', '_selected_action': [member.pk]})  # leaves a history entry
    from django.contrib.admin.models import CHANGE
    LogEntry.objects.log_actions(  # as "Send the invitation again" would: its name carries the address
        user_id=other_staff.pk, queryset=PatientInvitation.objects.filter(pk=invitation.pk), action_flag=CHANGE,
        change_message='Sent again', single_object=True)
    page = support.get('/admin/patient_portal/identity/').content
    assert b'close_member_account' not in page  # not without its permission

    Group.objects.get(name='Member support').permissions.add(Permission.objects.get(codename='close_member_accounts'))
    asked = support.post('/admin/patient_portal/identity/', {
        'action': 'close_member_account', '_selected_action': [member.pk]})
    assert asked.status_code == 200 and b'Type the member' in asked.content

    support.post('/admin/patient_portal/identity/', {
        'action': 'close_member_account', '_selected_action': [member.pk], 'confirm': 'close',
        'confirm_email': 'someone.else@example.test'})
    assert Person.objects.filter(person_id=990020).exists()

    support.post('/admin/patient_portal/identity/', {
        'action': 'close_member_account', '_selected_action': [other_staff.pk], 'confirm': 'close',
        'confirm_email': 'other@example.test'})
    assert Identity.objects.filter(pk=other_staff.pk).exists()  # never a staff account

    support.post('/admin/patient_portal/identity/', {
        'action': 'close_member_account', '_selected_action': [member.pk], 'confirm': 'close',
        'confirm_email': 'Leaving@Example.test'})
    assert not Person.objects.filter(person_id=990020).exists()
    assert not Identity.objects.filter(pk=member.pk).exists()
    names = set(LogEntry.objects.filter(object_id__in=[str(member.pk), str(invitation.pk)])
                .values_list('object_repr', flat=True))
    assert names == {'closed account (person 990020)'}  # the history stays, without the address
    assert LogEntry.objects.filter(change_message="Closed the account on the member's behalf").exists()


def test_a_withdrawal_asked_for_before_the_member_gave_it_again_is_refused():
    client, _ = staff_client('Member support')
    member, person = make_member('regranted@example.test', 990040)
    purpose = ConsentPurpose.objects.create(key='updates')
    ConsentText.objects.create(purpose=purpose, version='1', title='Updates from us', body='...',
                               published_at=timezone.now() - timezone.timedelta(days=30))
    ConsentEvent.objects.create(person=person, purpose=purpose, version='1', decision=ConsentEvent.GRANTED,
                                recorded_at=timezone.now())

    page = client.post('/admin/patient_portal/identity/', {
        'action': 'withdraw_permission', '_selected_action': [member.pk], 'confirm': 'withdraw',
        'purpose': 'updates', 'requested_on': (timezone.localdate() - timezone.timedelta(days=3)).isoformat()})

    assert b'after the date they asked' in page.content
    assert ConsentEvent.objects.filter(person=person).count() == 1


def test_dates_are_in_staffs_own_time_zone(settings):
    from patient_portal.admin import _staff_today
    from zoneinfo import ZoneInfo

    settings.STAFF_TIME_ZONE = 'Pacific/Kiritimati'  # UTC+14: often a day ahead of the server
    assert _staff_today() == timezone.localdate(timezone=ZoneInfo('Pacific/Kiritimati'))


def test_a_superuser_never_closes_a_staff_account_and_a_kept_sign_in_is_said():
    from omop_core.models import GroupAccess, Organization
    from patient_portal.erasure import sign_in_to_close

    client, _ = superuser_client()
    staff_member, staff_person = make_member('staff.member@example.test', 990041)
    staff_member.groups.add(Group.objects.get(name='Member support'))
    assert sign_in_to_close(staff_person) is None  # role staff keep their sign-in

    client.post('/admin/patient_portal/identity/', {
        'action': 'close_member_account', '_selected_action': [staff_member.pk], 'confirm': 'close',
        'confirm_email': 'staff.member@example.test'})
    assert Identity.objects.filter(pk=staff_member.pk).exists()

    provider, provider_person = make_member('doctor.member@example.test', 990042)
    GroupAccess.objects.create(identity=provider, org=Organization.objects.get(slug='flf-test'), role='doctor')
    page = client.post('/admin/patient_portal/identity/', {
        'action': 'close_member_account', '_selected_action': [provider.pk], 'confirm': 'close',
        'confirm_email': 'doctor.member@example.test'}, follow=True)
    assert b'The sign-in stays' in page.content
    assert Identity.objects.filter(pk=provider.pk).exists()


def test_the_access_log_hides_addresses_of_machines_and_records_its_own_reading():
    from patient_portal.models import AuditEvent

    Group.objects.get(name='Staff manager').permissions.add(Permission.objects.get(codename='view_auditevent'))
    client, _ = staff_client('Staff manager')
    AuditEvent.objects.create(event_type=AuditEvent.EVENT_VIEW, method='GET', path='/api/v1/x/',
                              ip_address='203.0.113.9', resource_id='990050')

    page = client.get('/admin/patient_portal/auditevent/', {'q': '990050'})

    assert page.status_code == 200 and b'990050' in page.content and b'203.0.113.9' not in page.content
    review = AuditEvent.objects.filter(event_type=AuditEvent.EVENT_AUDIT_REVIEW).order_by('-timestamp').first()
    assert review is not None and review.detail == {'audit_search': {'q': '990050'}}


def test_a_staff_change_is_in_the_members_download():
    from patient_portal.consent import consent_fhir_resources, record_consent

    member, person = make_member('download@example.test', 990043)
    purpose = ConsentPurpose.objects.create(key='updates')
    ConsentText.objects.create(purpose=purpose, version='1', title='Updates', body='...',
                               published_at=timezone.now() - timezone.timedelta(days=30))
    record_consent(person, purpose, ConsentEvent.GRANTED, version='1')
    record_consent(person, purpose, ConsentEvent.WITHDRAWN, source=ConsentEvent.STAFF,
                   requested_on=timezone.localdate(), note='by email')

    withdrawn = [r for r in consent_fhir_resources(person, 'Patient/1') if 'extension' in r]
    assert [e['url'] for e in withdrawn[0]['extension']] == ['urn:promop:consent-requested-on', 'urn:promop:consent-note']
