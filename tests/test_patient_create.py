"""An org admin creates a patient record, invites the patient or claims it (#2773)."""
import pytest
from django.core import mail
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, Organization, PatientRecord, Person
from patient_portal.models import Identity, PatientInvitation, PatientUser

pytestmark = pytest.mark.django_db

URL = '/api/v1/patients/'


def _org(slug='hosp'):
    return Organization.objects.create(name=slug.title(), slug=slug)


def _admin(org, email='admin@hosp.test', verified=True):
    user = Identity.objects.create_user(email=email, password='pw-Strong-12345')
    if verified:
        user.mark_email_verified()
    GroupAccess.objects.create(identity=user, org=org, role='org_admin')
    return user


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def test_admin_creates_and_invites_patient():
    org = _org()
    admin = _admin(org)
    mail.outbox = []
    resp = _client(admin).post(URL, {
        'organization': 'hosp', 'given_name': 'Rae', 'family_name': 'Reed',
        'email': 'Rae@Example.com', 'invite': True,
    }, format='json')
    assert resp.status_code == 201, resp.data
    person = Person.objects.get(person_id=resp.data['person_id'])
    assert person.email == 'rae@example.com'
    assert PatientRecord.objects.get(person=person).organization == org
    inv = PatientInvitation.objects.get(person=person)
    assert inv.email == 'rae@example.com' and inv.invited_by == admin
    assert len(mail.outbox) == 1 and inv.token in mail.outbox[0].body
    assert not PatientUser.objects.filter(person=person).exists()


def test_invited_patient_can_set_password():
    org = _org()
    resp = _client(_admin(org)).post(URL, {
        'given_name': 'Rae', 'email': 'rae@example.com', 'invite': True}, format='json')
    token = PatientInvitation.objects.get(person_id=resp.data['person_id']).token
    accepted = APIClient().post('/api/v1/patient-invitations/accept/',
                                {'token': token, 'password': 'Sup3r-secret-pw-91'}, format='json')
    assert accepted.status_code == 200, accepted.data
    assert PatientUser.objects.get(person_id=resp.data['person_id']).identity.email == 'rae@example.com'


def test_organization_optional_with_single_admin_org():
    _admin(_org())
    resp = _client(Identity.objects.get(email='admin@hosp.test')).post(
        URL, {'given_name': 'A'}, format='json')
    assert resp.status_code == 201
    assert resp.data['invited'] is False


def test_organization_required_with_several():
    admin = _admin(_org('a'))
    GroupAccess.objects.create(identity=admin, org=_org('b'), role='org_admin')
    assert _client(admin).post(URL, {'given_name': 'A'}, format='json').status_code == 400


def test_other_organization_refused():
    admin = _admin(_org('a'))
    _org('b')
    resp = _client(admin).post(URL, {'organization': 'b', 'given_name': 'A'}, format='json')
    assert resp.status_code == 403
    assert not Person.objects.exists()


@pytest.mark.parametrize('role', ['doctor', 'analyst', 'patient'])
def test_non_admin_roles_refused(role):
    org = _org()
    user = Identity.objects.create_user(email='x@hosp.test', password='pw-Strong-12345')
    GroupAccess.objects.create(identity=user, org=org, role=role)
    assert _client(user).post(URL, {'given_name': 'A'}, format='json').status_code == 403


def test_anonymous_refused():
    assert APIClient().post(URL, {'given_name': 'A'}, format='json').status_code in (401, 403)


def test_invite_needs_valid_email_and_leaves_no_record():
    admin = _admin(_org())
    c = _client(admin)
    for email in ('', 'not-an-email'):
        assert c.post(URL, {'given_name': 'A', 'email': email, 'invite': True},
                      format='json').status_code == 400
    assert not Person.objects.exists()


def test_invite_refused_for_email_with_existing_account():
    admin = _admin(_org())
    Identity.objects.create_user(email='taken@example.com', password='pw-Strong-12345')
    resp = _client(admin).post(URL, {'given_name': 'A', 'email': 'taken@example.com',
                                     'invite': True}, format='json')
    assert resp.status_code == 400
    assert not Person.objects.exists()


def test_empty_request_refused():
    assert _client(_admin(_org())).post(URL, {}, format='json').status_code == 400


def test_admin_creates_record_for_self_and_gains_patient_role():
    org = _org()
    admin = _admin(org)
    resp = _client(admin).post(URL, {'for_self': True, 'given_name': 'Ada'}, format='json')
    assert resp.status_code == 201, resp.data
    pu = PatientUser.objects.get(identity=admin)
    assert pu.person_id == resp.data['person_id']
    assert PatientRecord.objects.get(person=pu.person).organization == org
    roles = set(GroupAccess.objects.filter(identity=admin, org=org).values_list('role', flat=True))
    assert roles == {'org_admin', 'patient'}
    assert pu.person.email == 'admin@hosp.test'
    assert not PatientInvitation.objects.exists()


def test_self_record_leaves_unverified_email_off_person():
    admin = _admin(_org(), verified=False)
    resp = _client(admin).post(URL, {'for_self': True}, format='json')
    assert resp.status_code == 201
    assert Person.objects.get(person_id=resp.data['person_id']).email is None


def test_self_record_only_once():
    c = _client(_admin(_org()))
    assert c.post(URL, {'for_self': True, 'given_name': 'Ada'}, format='json').status_code == 201
    assert c.post(URL, {'for_self': True, 'given_name': 'Ada'}, format='json').status_code == 409
    assert Person.objects.count() == 1


def test_self_and_invite_together_refused():
    c = _client(_admin(_org()))
    resp = c.post(URL, {'for_self': True, 'invite': True, 'email': 'a@b.co'}, format='json')
    assert resp.status_code == 400


@pytest.mark.parametrize('days,says', [(1, '1 day'), (7, '7 days'), (30, '30 days')])
def test_an_invitation_lasts_the_deployments_invite_ttl(settings, days, says):
    """INVITE_TTL_DAYS (#2780): the link's expiry, and what the email says of it."""
    from django.utils import timezone

    settings.INVITE_TTL_DAYS = days
    mail.outbox = []
    resp = _client(_admin(_org())).post(URL, {
        'given_name': 'Rae', 'email': 'rae@example.com', 'invite': True}, format='json')
    assert resp.status_code == 201, resp.data
    inv = PatientInvitation.objects.get(person_id=resp.data['person_id'])
    left = inv.expires_at - timezone.now()
    assert days - 1 < left.total_seconds() / 86400 <= days
    assert f'This link expires in {says}.' in mail.outbox[0].body


def deployment_invitation(invitation, accept_url):
    """A deployment's own invitation email (PATIENT_INVITATION_EMAIL)."""
    return f'Join us, {invitation.email}', f'Our wording.\n\n{accept_url}\n'


def test_a_deployment_writes_its_own_invitation_email(settings):
    """PATIENT_INVITATION_EMAIL: the deployment's subject and body, with the
    same link; nothing of PROMOP's own wording."""
    settings.PATIENT_INVITATION_EMAIL = 'tests.test_patient_create.deployment_invitation'
    settings.APP_BASE_URL = 'https://members.example.org'
    mail.outbox = []
    resp = _client(_admin(_org())).post(URL, {
        'given_name': 'Rae', 'email': 'rae@example.com', 'invite': True}, format='json')
    assert resp.status_code == 201, resp.data
    inv = PatientInvitation.objects.get(person_id=resp.data['person_id'])
    sent = mail.outbox[0]
    assert sent.subject == 'Join us, rae@example.com'
    assert sent.body == f'Our wording.\n\nhttps://members.example.org/accept-patient-invite?token={inv.token}\n'
    assert 'PROMOP' not in sent.subject + sent.body


@pytest.mark.parametrize('raw', ['0', '91', '3000', 'thirty', ''])
def test_an_invite_ttl_outside_1_to_90_days_stops_startup(raw):
    from django.core.exceptions import ImproperlyConfigured

    from promop.settings import _invite_ttl_days

    with pytest.raises(ImproperlyConfigured, match='INVITE_TTL_DAYS'):
        _invite_ttl_days(raw)


def test_the_invite_ttl_defaults_to_7_days_and_takes_up_to_90():
    from promop.settings import _invite_ttl_days

    assert [_invite_ttl_days(v) for v in ('7', '1', '90', ' 30 ')] == [7, 1, 90, 30]
