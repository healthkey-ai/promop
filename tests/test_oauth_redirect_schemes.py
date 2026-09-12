"""Validate OAuth application redirects against freshly loaded runtime settings."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('debug', [False, True])
@pytest.mark.parametrize('allow_local_http', [False, True])
def test_oauth_redirect_validation(debug, allow_local_http):
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ}
    env.update(
        PYTHON_DOTENV_DISABLED='1',
        DJANGO_SETTINGS_MODULE='ctomop.settings',
        DEBUG=str(debug),
        SECRET_KEY='test-oauth-settings-key-never-used-in-a-deployment',
        DATABASE_URL='postgresql://postgres@localhost/promop_test',
        ALLOWED_HOSTS='app.example.invalid',
        CORS_ALLOWED_ORIGINS='https://app.example.invalid',
        ENVIRONMENT='local' if allow_local_http else 'production',
    )
    if allow_local_http:
        env['ALLOWED_REDIRECT_URI_SCHEMES'] = 'https, http'

    result = subprocess.run(
        [sys.executable, '-c', '''
import json
import django
django.setup()
from django.core.exceptions import ValidationError
from oauth2_provider.models import get_application_model

Application = get_application_model()
accepted = {}
for uri in (
    'https://client.example.invalid/callback',
    'http://client.example.invalid/callback',
    'http://localhost:3000/callback',
    'ftp://client.example.invalid/callback',
):
    application = Application(
        client_type=Application.CLIENT_PUBLIC,
        authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
        redirect_uris=uri,
    )
    try:
        application.clean()
    except ValidationError:
        accepted[uri] = False
    else:
        accepted[uri] = True
print(json.dumps(accepted))
'''],
        cwd=Path(__file__).resolve().parents[1], env=env,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        'https://client.example.invalid/callback': True,
        'http://client.example.invalid/callback': allow_local_http,
        'http://localhost:3000/callback': allow_local_http,
        'ftp://client.example.invalid/callback': False,
    }
