"""Application lifecycle, token secrecy, revocation, and the staff boundary."""
import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIClient, APIRequestFactory

from patient_portal.api.authentication import ServiceTokenAuthentication
from patient_portal.models import AuditEvent, Identity, ServiceAccessToken, ServiceApplication
from patient_portal.service_applications import issue_token, token_digest

URL = '/api/v1/service-applications/'


@pytest.fixture
def staff(db):
    return Identity.objects.create_user('token-admin@example.test', is_staff=True)


@pytest.fixture
def client(staff):
    client = APIClient()
    client.force_authenticate(user=staff)
    return client


def authenticate(secret):
    request = APIRequestFactory().get('/', HTTP_AUTHORIZATION=f'Bearer {secret}')
    return ServiceTokenAuthentication().authenticate(request)


def create_app(client):
    response = client.post(URL, {'name': 'A named app', 'service_id': 'named-app',
                                'owner_contact': 'Owner', 'scopes': 'patient/*.read'}, format='json')
    assert response.status_code == 201, response.data
    return response.data['id']


def test_lifecycle_only_discloses_secret_once_and_preserves_principal(client, staff):
    app_id = create_app(client)
    response = client.post(f'{URL}{app_id}/tokens/', {'label': 'First deployment'}, format='json')
    assert response.status_code == 201, response.data
    assert response['Cache-Control'] == 'no-store'
    secret = response.data['token']
    record = ServiceAccessToken.objects.get(pk=response.data['id'])
    assert record.digest == token_digest(secret)
    assert record.digest != secret and record.suffix == secret[-4:]
    assert record.created_by == staff
    listing = client.get(URL)
    assert secret not in listing.content.decode() and record.digest not in listing.content.decode()
    identity, credential = authenticate(secret)
    assert (identity.issuer, identity.sub, credential.scope) == ('urn:service', 'named-app', 'patient/*.read')
    record.refresh_from_db()
    assert record.last_used_at is not None
    response = client.patch(f'{URL}{app_id}/', {'name': 'Renamed', 'owner_contact': 'New owner',
                            'scopes': 'patient/*.write'}, format='json')
    assert response.status_code == 200
    assert authenticate(secret)[0].pk == identity.pk
    assert authenticate(secret)[1].scope == 'patient/*.write'
    assert client.patch(f'{URL}{app_id}/', {'service_id': 'changed'}, format='json').status_code == 400
    assert client.delete(f'{URL}{app_id}/').status_code == 405
    assert AuditEvent.objects.filter(path=f'{URL}{app_id}/tokens/', user_id=str(staff.pk), status_code=201).exists()
    assert secret not in str(list(AuditEvent.objects.values()))


def test_rotation_disable_expiry_and_revocation_do_not_fall_back_to_environment(client):
    app_id = create_app(client)
    first = client.post(f'{URL}{app_id}/tokens/', {'label': 'Old'}, format='json').data
    second = client.post(f'{URL}{app_id}/tokens/', {'label': 'Replacement'}, format='json').data
    assert authenticate(first['token']) and authenticate(second['token'])
    config = {'named-app': {'token': first['token'], 'scopes': 'patient/*.write'}}
    with override_settings(SERVICE_AUTH_TOKENS=config):
        result = client.post(f'{URL}{app_id}/tokens/{first["id"]}/revoke/')
        assert result.status_code == 200
        with pytest.raises(AuthenticationFailed):
            authenticate(first['token'])
        assert authenticate(second['token'])
        # Importing the old file cannot undo revocation (tested separately too).
        client.patch(f'{URL}{app_id}/', {'is_active': False}, format='json')
        with pytest.raises(AuthenticationFailed):
            authenticate(second['token'])
        assert client.post(f'{URL}{app_id}/tokens/', {'label': 'Disabled'}, format='json').status_code == 400
        client.patch(f'{URL}{app_id}/', {'is_active': True}, format='json')
        assert authenticate(second['token'])
        ServiceAccessToken.objects.filter(pk=second['id']).update(expires_at=timezone.now() - timedelta(seconds=1))
        with pytest.raises(AuthenticationFailed):
            authenticate(second['token'])
    with override_settings(SERVICE_AUTH_TOKENS={'named-app': {'token': 'unimported-env-key'}}):
        with pytest.raises(AuthenticationFailed):
            authenticate('unimported-env-key')


@pytest.mark.parametrize('identity_kind', ['anonymous', 'patient', 'org-admin', 'service', 'oauth-machine'])
def test_non_staff_and_machine_credentials_cannot_manage_tokens(db, identity_kind):
    client = APIClient()
    if identity_kind != 'anonymous':
        identity = Identity.objects.create_user(identity_kind + '@example.test')
        if identity_kind == 'service':
            identity.is_staff = True  # Still forbidden, even if forcibly misconfigured.
            client.force_authenticate(user=identity, token='service-token')
        elif identity_kind == 'oauth-machine':
            from oauth2_provider.models import Application, AccessToken
            identity.is_staff = True
            identity.save()
            app = Application.objects.create(name='Machine', user=identity,
                authorization_grant_type=Application.GRANT_CLIENT_CREDENTIALS)
            token = AccessToken.objects.create(application=app, user=identity, scope='patient/*.write',
                expires=timezone.now() + timedelta(hours=1), token='machine-token')
            client.force_authenticate(user=identity, token=token)
        else:
            if identity_kind == 'org-admin':
                from omop_core.models import GroupAccess, Organization
                org = Organization.objects.create(name='Org', slug='token-org')
                GroupAccess.objects.create(identity=identity, org=org, role='org_admin')
            client.force_authenticate(user=identity)
    for method, path in [('get', URL), ('post', URL), ('patch', URL + '1/'),
                         ('post', URL + '1/tokens/'), ('post', URL + '1/tokens/1/revoke/')]:
        response = getattr(client, method)(path, {}, format='json')
        assert response.status_code in (401, 403), (identity_kind, method, response.data)
    assert not ServiceAccessToken.objects.exists()


def test_session_staff_mutations_require_csrf(staff):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(staff)
    assert client.get(URL).status_code == 200
    assert client.post(URL, {'name': 'CSRF', 'service_id': 'csrf'}, format='json').status_code == 403


def test_wrong_application_and_invalid_input_rejected(client):
    app_id = create_app(client)
    other = ServiceApplication.objects.create(name='Other', service_id='other')
    token, _ = issue_token(other, 'Other token')
    assert client.post(f'{URL}{app_id}/tokens/{token.pk}/revoke/').status_code == 404
    assert client.post(f'{URL}{app_id}/tokens/', {'label': ''}, format='json').status_code == 400
    assert client.post(f'{URL}{app_id}/tokens/', {'label': 'Expired',
        'expires_at': (timezone.now() - timedelta(days=1)).isoformat()}, format='json').status_code == 400
    assert client.patch(f'{URL}{app_id}/', {'scopes': '*'}, format='json').status_code == 400


def test_import_preserves_distributed_keys_and_does_not_restore_revoked_access(db, tmp_path):
    config = {key: {'token': key + 'a' * 48, 'scopes': 'patient/*.read system/etl.write'}
              for key in ['etl', 'ht-phr', 'hk-labs', 'exact']}
    path = tmp_path / 'private.json'
    path.write_text(json.dumps(config))
    stdout = StringIO()
    call_command('import_service_tokens', file=str(path), stdout=stdout)
    assert ServiceApplication.objects.count() == ServiceAccessToken.objects.count() == 4
    for service_id, grant in config.items():
        assert authenticate(grant['token'])[0].sub == service_id
        assert grant['token'] not in stdout.getvalue()
    app = ServiceApplication.objects.get(service_id='etl')
    app.name = 'Edited ETL'; app.is_active = False; app.scopes = ''
    app.save()
    token = app.tokens.get(); token.revoked_at = timezone.now(); token.save()
    call_command('import_service_tokens', file=str(path), stdout=stdout)
    app.refresh_from_db(); token.refresh_from_db()
    assert app.name == 'Edited ETL' and app.scopes == '' and not app.is_active
    assert token.revoked_at is not None
    assert ServiceAccessToken.objects.count() == 4
    with override_settings(SERVICE_AUTH_TOKENS=config), pytest.raises(AuthenticationFailed):
        authenticate(config['etl']['token'])


def test_invalid_import_rolls_back_the_entire_batch(db, tmp_path):
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps({'good': {'token': 'x' * 48}, 'bad': {'token': 'short'}}))
    with pytest.raises(CommandError):
        call_command('import_service_tokens', file=str(path))
    assert not ServiceApplication.objects.exists()
    assert not ServiceAccessToken.objects.exists()
