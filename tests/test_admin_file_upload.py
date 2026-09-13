"""Administrative upload entry points, tenant isolation and FHIR ordering."""
import copy
import io
import json
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.models import GroupAccess, Organization, Person, PatientRecord, ConditionOccurrence, ProvenanceRecord
from patient_portal.models import Identity
from patient_portal.tests import _make_vocab_fixtures, _make_fhir_bundle

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup():
    _make_vocab_fixtures()
    org = Organization.objects.create(name='Upload clinic', slug='upload-clinic')
    user = Identity.objects.create_user(email='uploader@example.test', password='password')
    GroupAccess.objects.create(identity=user, org=org, role='org_admin')
    client = APIClient()
    client.force_authenticate(user=user)
    return client, user, org


def upload(client, bundle=None, **data):
    file = io.BytesIO(json.dumps(bundle if bundle is not None else _make_fhir_bundle()).encode())
    file.name = 'bundle.JSON'
    return client.post('/api/patient-info/upload_fhir/', {'file': file, **data}, format='multipart')


def test_org_admin_imports_unordered_bundle_and_reimports(setup):
    client, user, org = setup
    bundle = _make_fhir_bundle()
    bundle['entry'].reverse()
    result = upload(client, bundle, source_user_id='forged-user')
    assert result.status_code == 200, result.data
    assert result.data['errors'] == []
    assert result.data['created_count'] == 1
    person = Person.objects.get(given_name='Jane', family_name='Smith')
    assert PatientRecord.objects.get(person=person).organization == org
    assert ConditionOccurrence.objects.filter(person=person).exists()
    assert ProvenanceRecord.objects.filter(source_user_id=str(user.pk), organization=org).exists()
    assert not ProvenanceRecord.objects.filter(source_user_id='forged-user').exists()
    again = upload(client, bundle)
    assert again.data['created_count'] == 0
    assert again.data['updated_count'] == 1
    assert again.data['errors'] == []


def test_org_admin_cannot_update_other_tenant_or_select_it(setup):
    client, user, org = setup
    other = Organization.objects.create(name='Other', slug='other-upload')
    user.is_staff = True
    user.save()
    assert upload(client, organization=other.slug).data['created_count'] == 1
    user.is_staff = False
    user.save()
    person = Person.objects.get(given_name='Jane', family_name='Smith')
    before = ConditionOccurrence.objects.filter(person=person).count()
    result = upload(client)
    assert result.data['created_count'] == result.data['updated_count'] == 0
    assert result.data['errors']
    assert PatientRecord.objects.get(person=person).organization == other
    assert ConditionOccurrence.objects.filter(person=person).count() == before
    assert upload(client, organization=other.slug).status_code == 403


@pytest.mark.parametrize('kind', ['fhir', 'csv'])
@pytest.mark.parametrize('role', ['doctor', 'analyst', 'patient', 'expired_admin'])
def test_only_current_admins_can_upload(setup, kind, role):
    client, user, org = setup
    grant = GroupAccess.objects.get(identity=user)
    if role == 'expired_admin':
        grant.expires_at = timezone.now() - timedelta(seconds=1)
    else:
        grant.role = role
    grant.save()
    assert client.post(f'/api/patient-info/upload_{kind}/', {}, format='multipart').status_code == 403


@pytest.mark.parametrize('kind', ['fhir', 'csv'])
def test_staff_and_org_admin_are_allowed_but_multiple_orgs_need_selection(setup, kind):
    client, user, org = setup
    other = Organization.objects.create(name='Second', slug='second-upload')
    GroupAccess.objects.create(identity=user, org=other, role='org_admin')
    response = client.post(f'/api/patient-info/upload_{kind}/', {}, format='multipart')
    assert response.status_code == 400
    assert 'Select an organization' in str(response.data)
    user.is_staff = True
    user.save()
    response = client.post(f'/api/patient-info/upload_{kind}/', {}, format='multipart')
    assert response.data == {'error': 'No file provided'}


@pytest.mark.parametrize('problem', ['empty', 'duplicate', 'orphan', 'missing_id', 'list', 'bad_entry'])
def test_invalid_bundle_rejected_before_writes(setup, problem):
    client, _, _ = setup
    bundle = _make_fhir_bundle()
    patient = next(e for e in bundle['entry'] if e['resource']['resourceType'] == 'Patient')
    if problem == 'empty':
        bundle['entry'] = []
    elif problem == 'duplicate':
        bundle['entry'].append(copy.deepcopy(patient))
    elif problem == 'orphan':
        bundle['entry'].append({'resource': {'resourceType': 'Observation', 'subject': {'reference': 'Patient/absent'}}})
    elif problem == 'missing_id':
        patient['resource'].pop('id')
    elif problem == 'list':
        bundle = []
    else:
        bundle['entry'].append(None)
    response = upload(client, bundle)
    assert response.status_code == 400, response.data
    assert Person.objects.count() == 0


def test_csv_new_and_existing_patient_counts_and_org(setup):
    client, _, org = setup
    def post():
        file = io.BytesIO(b'person_id,given_name,family_name,date_of_birth\n190001,CSV,Example,1980-01-01\n')
        file.name = 'patients.CSV'
        return client.post('/api/patient-info/upload_csv/', {'file': file}, format='multipart')
    first = post()
    assert first.data['errors'] == [], first.data
    assert first.data['created_count'] == 1
    assert PatientRecord.objects.get(person_id=190001).organization == org
    second = post()
    assert second.data['created_count'] == 0
    assert second.data['updated_count'] == 1


def test_session_upload_enforces_csrf(setup):
    _, user, _ = setup
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    assert upload(client).status_code == 403


def test_session_admin_upload_with_csrf_succeeds(setup):
    from django.middleware.csrf import _get_new_csrf_string
    _, user, org = setup
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    csrf = _get_new_csrf_string()
    client.cookies['csrftoken'] = csrf
    client.credentials(HTTP_X_CSRFTOKEN=csrf)
    response = upload(client)
    assert response.status_code == 200, response.data
    assert response.data['created_count'] == 1
    assert PatientRecord.objects.get(person__given_name='Jane').organization == org


def test_csv_cannot_claim_an_existing_unassigned_patient(setup):
    client, _, _ = setup
    person = Person.objects.create(person_id=190002, given_name='Original')
    file = io.BytesIO(b'person_id,given_name\n190002,Overwrite\n')
    file.name = 'patients.csv'
    response = client.post('/api/patient-info/upload_csv/', {'file': file}, format='multipart')
    assert response.data['errors']
    person.refresh_from_db()
    assert person.given_name == 'Original'


def test_invalid_patient_does_not_prevent_later_patient_import(setup):
    client, _, _ = setup
    bundle = _make_fhir_bundle()
    bad = {'resource': {'resourceType': 'Patient', 'id': 'bad-date', 'birthDate': 'invalid'}}
    bundle['entry'].insert(0, bad)
    response = upload(client, bundle)
    assert response.status_code == 200, response.data
    assert response.data['created_count'] == 1
    assert len(response.data['errors']) == 1
    assert Person.objects.count() == 1


@pytest.mark.parametrize('scope,expected', [('patient/*.read', 403), ('patient/*.write', 200)])
def test_human_oauth_admin_still_needs_write_scope(setup, scope, expected):
    from oauth2_provider.models import Application, AccessToken
    client, user, _ = setup
    app = Application.objects.create(name='Admin browser', client_type=Application.CLIENT_CONFIDENTIAL,
                                     authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE, user=user)
    token = AccessToken.objects.create(application=app, user=user, token=f'upload-test-{scope}',
                                       scope=scope, expires=timezone.now() + timedelta(hours=1))
    client.force_authenticate(user=user, token=token)
    response = upload(client)
    assert response.status_code == expected, response.data


def test_trust_admin_can_upload_into_trusting_organization(setup):
    from omop_core.models import OrgTrust
    client, user, org = setup
    grant = GroupAccess.objects.get(identity=user)
    grant.role = 'doctor'
    grant.save()
    trusting = Organization.objects.create(name='Trusting clinic', slug='trusting-upload')
    OrgTrust.objects.create(granting_org=trusting, trusted_org=org)
    response = upload(client, organization=trusting.slug)
    assert response.status_code == 200, response.data
    assert response.data['created_count'] == 1
    assert PatientRecord.objects.get(person__given_name='Jane').organization == trusting
