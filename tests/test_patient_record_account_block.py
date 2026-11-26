"""The ``user`` block of a patient record read (#2845).

``GET /api/v1/patient-records/<id>/`` returns the account linked to the
record, shaped by what the reader is to that account. The legacy
``/api/patient-info/<id>/`` keeps the full block for every reader.
"""
import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, OrgTrust
from patient_portal.models import Identity, PatientUser
from tests.factories import OrganizationFactory, PatientRecordFactory

pytestmark = pytest.mark.django_db

FULL_ONLY = {'sub', 'is_staff', 'is_superuser', 'is_org_admin', 'org_accesses', 'is_patient',
             'person_id', 'effective_roles', 'patient_delegations', 'must_change_password',
             'email_verified'}
REDUCED = {'id', 'name', 'email'}
TOKEN = 'account-block-secret'
SERVICE = {'etl': {'token': TOKEN, 'scopes': 'patient/*.read'}}


def _identity(email, **extra):
    return Identity.objects.create_user(email=email, **extra)


def _client(identity=None):
    client = APIClient()
    if identity is not None:
        client.force_authenticate(identity)
    return client


def _grant(identity, org, role):
    return GroupAccess.objects.create(identity=identity, org=org, role=role)


@pytest.fixture
def world():
    org = OrganizationFactory()
    record = PatientRecordFactory(organization=org)
    account = _identity('pat-account@example.test', name='Pat Account')
    PatientUser.objects.create(identity=account, person=record.person)
    # The account holds a grant elsewhere: this is what must not travel.
    _grant(account, OrganizationFactory(name='Elsewhere'), 'doctor')
    return org, record, account


def _read(client, record, prefix='/api/v1/patient-records'):
    response = client.get(f'{prefix}/{record.person_id}/')
    return response


def _user(client, record, **kw):
    response = _read(client, record, **kw)
    assert response.status_code == 200, response.content
    return response.data['user']


def _suppress(record):
    record.suppress_demographics_for_others = True
    record.save()


def test_own_account_gets_the_full_block(world):
    org, record, account = world
    user = _user(_client(account), record)
    assert FULL_ONLY <= set(user)
    assert user['name'] == 'Pat Account' and user['email'] == 'pat-account@example.test'
    _suppress(record)
    assert _user(_client(account), record)['name'] == 'Pat Account'


def test_staff_gets_the_full_block_and_suppression_withholds_name_and_email(world):
    org, record, account = world
    staff = _identity('staff@example.test', is_staff=True)
    user = _user(_client(staff), record)
    assert FULL_ONLY <= set(user) and user['email'] == 'pat-account@example.test'
    _suppress(record)
    user = _user(_client(staff), record)
    assert FULL_ONLY <= set(user)
    assert user['name'] is None and user['email'] is None


def test_org_admin_of_the_records_org_gets_the_full_block(world):
    org, record, account = world
    admin = _identity('admin@example.test')
    _grant(admin, org, 'org_admin')
    user = _user(_client(admin), record)
    assert FULL_ONLY <= set(user)
    _suppress(record)
    user = _user(_client(admin), record)
    assert user['name'] is None and user['email'] is None


def test_trust_admin_gets_identity_only(world):
    org, record, account = world
    trusted = OrganizationFactory()
    OrgTrust.objects.create(granting_org=org, trusted_org=trusted)
    admin = _identity('trust-admin@example.test')
    _grant(admin, trusted, 'org_admin')
    response = _read(_client(admin), record)
    assert response.status_code == 200
    assert set(response.data['user']) == REDUCED


def test_doctor_gets_identity_only(world):
    org, record, account = world
    doctor = _identity('doctor@example.test')
    _grant(doctor, org, 'doctor')
    user = _user(_client(doctor), record)
    assert set(user) == REDUCED
    assert user['name'] == 'Pat Account' and user['email'] == 'pat-account@example.test'
    assert user['id'] == account.pk


def test_doctor_sees_nothing_of_a_suppressed_record_account(world):
    org, record, account = world
    _suppress(record)
    doctor = _identity('doctor@example.test')
    _grant(doctor, org, 'doctor')
    user = _user(_client(doctor), record)
    assert user == {'id': account.pk, 'name': None, 'email': None}
    assert _read(_client(doctor), record).data['patient_info']['demographics_redacted'] is True


def test_doctor_of_another_org_and_analyst_get_no_record(world):
    org, record, account = world
    other_doctor = _identity('other-doctor@example.test')
    _grant(other_doctor, OrganizationFactory(), 'doctor')
    analyst = _identity('analyst@example.test')
    _grant(analyst, org, 'analyst')
    assert _read(_client(other_doctor), record).status_code == 404
    assert _read(_client(analyst), record).status_code == 404


@override_settings(SERVICE_AUTH_TOKENS=SERVICE, SERVICE_AUTH_TOKEN='')
def test_service_token_gets_identity_only_and_suppression_applies(world):
    org, record, account = world
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {TOKEN}')
    user = _user(client, record)
    assert set(user) == REDUCED and user['email'] == 'pat-account@example.test'
    _suppress(record)
    assert _user(client, record) == {'id': account.pk, 'name': None, 'email': None}


def test_machine_identity_with_admin_standing_is_still_reduced(world):
    org, record, account = world
    machine = _identity('machine@example.test', is_staff=True)
    Identity.objects.filter(pk=machine.pk).update(issuer='urn:service')
    machine.refresh_from_db()
    assert set(_user(_client(machine), record)) == REDUCED


def test_record_without_a_linked_account_has_no_user_block():
    record = PatientRecordFactory()
    staff = _identity('staff@example.test', is_staff=True)
    assert _user(_client(staff), record) is None


def test_legacy_path_keeps_the_full_block_for_a_doctor(world):
    org, record, account = world
    doctor = _identity('doctor@example.test')
    _grant(doctor, org, 'doctor')
    user = _user(_client(doctor), record, prefix='/api/patient-info')
    assert FULL_ONLY <= set(user)
