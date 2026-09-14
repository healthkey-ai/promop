"""Boot real settings in clean subprocesses so DEBUG cannot hide weak defaults."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASE_ENV = {
    'PYTHON_DOTENV_DISABLED': '1',
    'DJANGO_SETTINGS_MODULE': 'ctomop.settings',
    'SECRET_KEY': 'test-settings-key-that-is-never-used-by-a-deployment',
    'DATABASE_URL': 'postgresql://postgres@localhost/promop_test',
    'ALLOWED_HOSTS': 'app.example.invalid',
    'CORS_ALLOWED_ORIGINS': 'https://app.example.invalid',
}


def boot(debug, argv=None, **overrides):
    code = f'''
import sys
sys.argv = {argv or ['gunicorn']!r}
from django.conf import settings
from patient_portal.checks import security_posture_check
print(security_posture_check(None)[0].msg.split(': ', 1)[1])
'''
    # Inherit only interpreter essentials: a developer's .env, emulator, or
    # security overrides must not make these assertions pass accidentally.
    env = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT') if key in os.environ}
    env.update(BASE_ENV, DEBUG=str(debug), **overrides)
    return subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        capture_output=True, text=True,
    )


@pytest.mark.parametrize('debug', [False, True])
def test_security_defaults_do_not_depend_on_debug(debug):
    result = boot(debug, RENDER='true')
    assert result.returncode == 0, result.stderr
    posture = json.loads(result.stdout)
    assert posture['DEBUG'] == debug
    for name in ('IS_DEPLOYED', 'SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE',
                 'SECURE_HSTS_INCLUDE_SUBDOMAINS', 'SECURE_HSTS_PRELOAD',
                 'SECURE_CONTENT_TYPE_NOSNIFF'):
        assert posture[name] is True
    for name in ('WILDCARD_HOSTS', 'CORS_ALLOW_ALL_ORIGINS', 'BASIC_AUTH_ENABLED',
                 'FIREBASE_SKIP_REVOCATION_CHECK', 'PHR_AUDIENCE_CONFIGURED',
                 'PHR_BASE_URL_CONFIGURED', 'FIREBASE_PROJECT_ID_CONFIGURED'):
        assert posture[name] is False
    assert posture['SECURE_HSTS_SECONDS'] == 31536000
    assert posture['X_FRAME_OPTIONS'] == 'DENY'
    assert posture['SECURE_PROXY_SSL_HEADER'] == ['HTTP_X_FORWARDED_PROTO', 'https']
    assert posture['ALLOWED_REDIRECT_URI_SCHEMES'] == ['https']


@pytest.mark.parametrize('debug', [False, True])
def test_each_control_can_be_explicitly_overridden(debug):
    result = boot(
        debug, ALLOWED_HOSTS='*', CORS_ALLOW_ALL_ORIGINS='true',
        CORS_ALLOW_CREDENTIALS='false', ENABLE_BASIC_AUTH='true',
        FIREBASE_SKIP_REVOCATION_CHECK='true', FIREBASE_PROJECT_ID='explicit-project',
        PHR_BASE_URL='http://localhost:9000', PHR_AUDIENCE='explicit-audience',
        SESSION_COOKIE_SECURE='false', CSRF_COOKIE_SECURE='false',
        SECURE_HSTS_SECONDS='0', SECURE_HSTS_INCLUDE_SUBDOMAINS='false',
        SECURE_HSTS_PRELOAD='false', SECURE_CONTENT_TYPE_NOSNIFF='false',
        X_FRAME_OPTIONS='SAMEORIGIN', TRUST_PROXY_SSL_HEADER='false',
        ALLOWED_REDIRECT_URI_SCHEMES='https, http', SECURE_SSL_REDIRECT='true',
    )
    assert result.returncode == 0, result.stderr
    posture = json.loads(result.stdout)
    for name in ('WILDCARD_HOSTS', 'CORS_ALLOW_ALL_ORIGINS', 'BASIC_AUTH_ENABLED',
                 'FIREBASE_SKIP_REVOCATION_CHECK', 'PHR_AUDIENCE_CONFIGURED',
                 'PHR_BASE_URL_CONFIGURED', 'FIREBASE_PROJECT_ID_CONFIGURED',
                 'SECURE_SSL_REDIRECT'):
        assert posture[name] is True
    for name in ('SESSION_COOKIE_SECURE', 'CSRF_COOKIE_SECURE', 'CORS_ALLOW_CREDENTIALS',
                 'SECURE_HSTS_INCLUDE_SUBDOMAINS', 'SECURE_HSTS_PRELOAD',
                 'SECURE_CONTENT_TYPE_NOSNIFF'):
        assert posture[name] is False
    assert posture['SECURE_HSTS_SECONDS'] == 0
    assert posture['X_FRAME_OPTIONS'] == 'SAMEORIGIN'
    assert posture['SECURE_PROXY_SSL_HEADER'] is None
    assert posture['ALLOWED_REDIRECT_URI_SCHEMES'] == ['https', 'http']


@pytest.mark.parametrize('marker', [
    {'RENDER': 'true'},
    {'RENDER_EXTERNAL_URL': 'https://app.example.invalid'},
    {'RENDER_EXTERNAL_HOSTNAME': 'app.example.invalid'},
    {'ENVIRONMENT': 'staging'},
    {'ENVIRONMENT': 'production'},
    {'ENVIRONMENT': 'local', 'RENDER': 'true'},
])
@pytest.mark.parametrize('argv', [['gunicorn'], ['manage.py', 'check', '--deploy']])
@pytest.mark.parametrize('missing,value', [
    ('SECRET_KEY', ''), ('SECRET_KEY', 'django-insecure-placeholder'),
    ('DATABASE_URL', ''), ('CORS_ALLOWED_ORIGINS', ' , '),
])
def test_deployed_debug_processes_validate_required_config(marker, argv, missing, value):
    result = boot(True, argv, **marker, **{missing: value})
    assert result.returncode != 0
    assert f'{missing} must be set' in result.stderr


def test_deployed_debug_requires_hosts_without_render_hostname():
    result = boot(True, ENVIRONMENT='staging', ALLOWED_HOSTS=' , ')
    assert result.returncode != 0
    assert 'ALLOWED_HOSTS must be set' in result.stderr


@pytest.mark.parametrize('command', ['collectstatic', 'migrate', 'check'])
def test_render_build_commands_remain_exempt(command):
    result = boot(True, ['manage.py', command], RENDER='true', SECRET_KEY='',
                  DATABASE_URL='', ALLOWED_HOSTS='', CORS_ALLOWED_ORIGINS='')
    assert result.returncode == 0, result.stderr


def test_render_worker_requires_secret_and_database_but_not_http_config():
    env = dict(RENDER='true', ALLOWED_HOSTS='', CORS_ALLOWED_ORIGINS='')
    result = boot(True, ['/usr/local/bin/celery'], **env)
    assert result.returncode == 0, result.stderr
    for missing in ('SECRET_KEY', 'DATABASE_URL'):
        result = boot(True, ['/usr/local/bin/celery'], **env, **{missing: ''})
        assert result.returncode != 0
        assert f'{missing} must be set' in result.stderr


def test_local_debug_needs_explicit_identity_config_and_does_not_trust_proxy():
    result = boot(True, SECRET_KEY='', DATABASE_URL='', ALLOWED_HOSTS='',
                  CORS_ALLOWED_ORIGINS='')
    assert result.returncode == 0, result.stderr
    posture = json.loads(result.stdout)
    assert posture['IS_DEPLOYED'] is False
    assert posture['PHR_AUDIENCE_CONFIGURED'] is False
    assert posture['FIREBASE_PROJECT_ID_CONFIGURED'] is False
    assert posture['SECURE_PROXY_SSL_HEADER'] is None
    # Local HTTP callbacks need the explicit override, never just a local env.
    assert posture['ALLOWED_REDIRECT_URI_SCHEMES'] == ['https']
