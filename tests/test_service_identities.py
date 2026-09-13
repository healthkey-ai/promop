"""Service authentication, actor trust boundaries, and credential provisioning."""
import json
import stat
from io import StringIO
from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient, APIRequestFactory

from omop_core.models import Measurement, Person, ProvenanceRecord, VisitOccurrence
from patient_portal.api.authentication import ServiceTokenAuthentication
from patient_portal.api.fhir.tests import SAMPLE_BUNDLE
from patient_portal.api.lab_results.tests import _setup_vocab
from patient_portal.api.permissions import EtlWritePermission, ScopedTokenPermission
from patient_portal.models import AuditEvent, Identity, PatientUser
from patient_portal.service_tokens import ServiceCredential, parse_service_tokens, service_credentials

GRANTS = {
    'etl': {'token': 'etl-test-secret', 'scopes': 'patient/*.read system/etl.write'},
    'hk-labs': {'token': 'labs-test-secret', 'scopes': 'patient/*.read patient/*.write'},
    'exact': {'token': 'exact-test-secret', 'scopes': 'patient/*.read patient/*.write'},
}


def authenticate(secret):
    request = APIRequestFactory().get('/', HTTP_AUTHORIZATION=f'Bearer {secret}')
    return ServiceTokenAuthentication().authenticate(request)


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='legacy-secret')
def test_distinct_principals_legacy_compatibility_and_scope_isolation():
    etl, grant = authenticate('etl-test-secret')
    labs, labs_grant = authenticate('labs-test-secret')
    legacy, _ = authenticate('legacy-secret')
    assert etl.issuer == labs.issuer == legacy.issuer == 'urn:service'
    assert len({etl.pk, labs.pk, legacy.pk}) == 3
    assert (etl.sub, labs.sub, legacy.sub) == ('etl', 'hk-labs', 'hk-labs-sync')
    assert not etl.has_usable_password()
    assert not etl.is_staff and not etl.is_superuser
    assert 'secret' not in repr(grant)
    request = SimpleNamespace(auth=grant, user=etl, method='POST')
    assert EtlWritePermission().has_permission(request, None)
    assert not ScopedTokenPermission().has_permission(request, None)
    request.method = 'DELETE'
    assert not EtlWritePermission().has_permission(request, None)
    request.auth = labs_grant
    assert ScopedTokenPermission().has_permission(request, None)


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_rotation_revocation_unknown_and_disabled_credentials():
    identity, _ = authenticate('labs-test-secret')
    changed = {**GRANTS, 'hk-labs': {**GRANTS['hk-labs'], 'token': 'new-test-secret'}}
    with override_settings(SERVICE_AUTH_TOKENS=changed):
        assert authenticate('labs-test-secret') is None
        assert authenticate('new-test-secret')[0].pk == identity.pk
        assert authenticate('etl-test-secret')[0].sub == 'etl'
    assert authenticate('unknown-secret') is None
    assert authenticate('non-ascii-\u00e9') is None
    identity.is_staff = identity.is_superuser = True
    identity.save()
    repaired, _ = authenticate('labs-test-secret')
    assert not repaired.is_staff and not repaired.is_superuser
    identity.is_active = False
    identity.save(update_fields=['is_active'])
    with pytest.raises(AuthenticationFailed):
        authenticate('labs-test-secret')


@pytest.mark.parametrize('config', [
    [], {'invalid|id': {'token': 'secret'}}, {'svc': {}},
    {'svc': {'token': ''}}, {'svc': {'token': 'white space'}},
    {'svc': {'token': '\u00e9'}}, {'svc': {'token': 'secret', 'scopes': []}},
    {'svc': {'token': 'secret', 'scope': 'typo'}},
    {'a': {'token': 'same'}, 'b': {'token': 'same'}},
])
def test_invalid_configuration_fails_closed_without_disclosing_secrets(config):
    with pytest.raises(ImproperlyConfigured) as error:
        service_credentials(config)
    assert 'white space' not in str(error.value)
    assert 'same' not in str(error.value)


def test_duplicate_legacy_credential_and_malformed_json_rejected():
    with pytest.raises(ImproperlyConfigured):
        service_credentials({'a': {'token': 'duplicate'}}, 'duplicate')
    with pytest.raises(ImproperlyConfigured):
        service_credentials({'hk-labs-sync': {'token': 'new'}}, 'old')
    with pytest.raises(ImproperlyConfigured):
        parse_service_tokens('invalid-json-secret')
    assert service_credentials({'read-only': {'token': 'read'}})[0][1].scope == 'patient/*.read'


@pytest.mark.django_db
@pytest.mark.parametrize('service_id', ['hk-labs', 'exact'])
@pytest.mark.parametrize('endpoint', ['lab-results', 'fhir'])
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_sync_records_actual_service_in_provenance_and_audit(service_id, endpoint):
    _setup_vocab()
    person = Person.objects.create(person_id=871001)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer ' + GRANTS[service_id]['token'])
    payload = {'person_id': person.pk}
    if endpoint == 'fhir':
        payload['bundle'] = SAMPLE_BUNDLE
    else:
        payload['measurements'] = [{'test_name': 'Hemoglobin', 'loinc_code': '718-7',
                                    'value': '12', 'measured_at': '2026-06-01'}]
    response = client.post(f'/api/{endpoint}/sync/', payload, format='json')
    assert response.status_code == 201, response.data
    measurement = Measurement.objects.get(person=person)
    assert ProvenanceRecord.objects.filter(
        content_type__model='measurement', object_id=measurement.pk,
        source_user_id=f'urn:service|{service_id}',
    ).exists()
    actor = Identity.objects.get(issuer='urn:service', sub=service_id)
    assert AuditEvent.objects.filter(path=f'/api/{endpoint}/sync/', user_id=str(actor.pk),
                                     status_code=201).exists()


@pytest.mark.django_db
@pytest.mark.parametrize('endpoint', ['lab-results', 'fhir'])
@pytest.mark.parametrize('explicit_patient', [False, True])
@pytest.mark.parametrize('known_actor', [False, True])
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_service_cannot_claim_or_provision_actor(endpoint, explicit_patient, known_actor):
    person = Person.objects.create(person_id=871002)
    if known_actor:
        actor = Identity.objects.create(issuer='https://issuer.example', sub='victim')
        PatientUser.objects.create(identity=actor, person=person)
    counts = (Identity.objects.count(), Person.objects.count(), PatientUser.objects.count())
    payload = {'actor_iss': 'https://issuer.example', 'actor_sub': 'victim'}
    if explicit_patient:
        payload['person_id'] = person.pk
    if endpoint == 'fhir':
        payload['bundle'] = SAMPLE_BUNDLE
    else:
        payload['measurements'] = [{'test_name': 'Hemoglobin', 'value': '12',
                                    'measured_at': '2026-06-01'}]
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer labs-test-secret')
    response = client.post(f'/api/{endpoint}/sync/', payload, format='json')
    assert response.status_code == 403, response.data
    assert Person.objects.count() == counts[1]
    assert PatientUser.objects.count() == counts[2]
    assert Identity.objects.exclude(issuer='urn:service').count() == counts[0]
    assert not Measurement.objects.exists()
    assert not VisitOccurrence.objects.exists()
    assert not ProvenanceRecord.objects.exists()


@pytest.mark.django_db
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_find_or_create_requires_the_authenticated_subject():
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer etl-test-secret')
    payload = {'actor_iss': 'https://issuer.example', 'actor_sub': 'victim'}
    response = client.post('/api/persons/find_or_create/', payload, format='json')
    assert response.status_code == 403
    assert not Person.objects.exists()
    user = Identity.objects.create(issuer='https://issuer.example', sub='actual-user')
    client.force_authenticate(user=user)
    response = client.post('/api/persons/find_or_create/', payload, format='json')
    assert response.status_code == 403
    assert not Person.objects.exists()


def test_generator_creates_private_file_without_logging_or_overwriting_secrets(tmp_path):
    output = tmp_path / 'service-tokens.json'
    stdout = StringIO()
    call_command('generate_service_tokens', '--service', 'etl=patient/*.read system/etl.write',
                 '--service', 'hk-labs=patient/*.write', '--service', 'exact=patient/*.read',
                 output=str(output), stdout=stdout)
    config = json.loads(output.read_text())
    assert len({entry['token'] for entry in config.values()}) == 3
    assert all(len(entry['token']) >= 64 for entry in config.values())
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert all(entry['token'] not in stdout.getvalue() for entry in config.values())
    with pytest.raises(CommandError):
        call_command('generate_service_tokens', service=['etl='], output=str(output))
    assert json.loads(output.read_text()) == config


@pytest.mark.django_db
@pytest.mark.parametrize('actor_fields', [
    {'actor_iss': 'https://issuer.example', 'actor_sub': 'victim'},
    {'actor_iss': 'https://issuer.example'}, {'actor_sub': 'victim'},
])
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_service_oidc_signup_cannot_provision_a_claimed_identity(actor_fields):
    from omop_core.models import Organization
    org = Organization.objects.create(name='Signup org', slug='signup-org')
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer labs-test-secret')
    response = client.post('/api/v1/patients/signup/',
                           {'org': org.slug, **actor_fields}, format='json')
    assert response.status_code == 403, response.data
    assert not Identity.objects.exclude(issuer='urn:service').exists()
    assert not PatientUser.objects.exists()
    assert not Person.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize('service_id', ['etl', 'hk-labs'])
@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_ordinary_clinical_provenance_cannot_spoof_service_actor(service_id):
    _setup_vocab()
    person = Person.objects.create(person_id=871003)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION='Bearer ' + GRANTS[service_id]['token'])
    response = client.post('/api/measurements/', {
        'person': person.pk, 'measurement_concept': 3000963,
        'measurement_date': '2026-06-01', 'measurement_type_concept': 32883,
        'value_as_number': 12, 'source': 'EHR_SYNC', 'source_user_id': 'spoofed-patient',
    }, format='json', HTTP_X_PROVENANCE_USER_ID='another-spoof')
    assert response.status_code == 201, response.data
    measurement = Measurement.objects.get(person=person)
    assert ProvenanceRecord.objects.filter(
        content_type__model='measurement', object_id=measurement.pk,
        source_user_id=f'urn:service|{service_id}', source='EHR_SYNC',
    ).exists()
