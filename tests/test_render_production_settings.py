"""Exercise production settings imports without a database or Render account."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENV = {
    'DJANGO_SETTINGS_MODULE': 'promop.settings',
    'PYTHON_DOTENV_DISABLED': '1',
    'DEBUG': 'False',
    'SECRET_KEY': 'test-production-settings-secret',
    'DATABASE_URL': 'postgresql://postgres@localhost/promop_test',
    'CORS_ALLOWED_ORIGINS': 'https://frontend.example.invalid',
    'ALLOWED_HOSTS': '',
    'RENDER_EXTERNAL_HOSTNAME': '',
}


def boot(argv, **overrides):
    code = (
        f'import sys; sys.argv = {argv!r}; '
        'from django.conf import settings; '
        'from django.http import HttpRequest; '
        'from django.core.exceptions import DisallowedHost; '
        'import json; '
        'request = HttpRequest(); '
        'request.META["HTTP_HOST"] = "untrusted.example.invalid"; '
        '\ntry:\n request.get_host()\n'
        'except DisallowedHost:\n pass\n'
        'else:\n raise AssertionError("Untrusted host accepted")\n'
        'print(json.dumps(settings.ALLOWED_HOSTS))'
    )
    return subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT,
        env={**os.environ, **ENV, **overrides}, capture_output=True, text=True,
    )


@pytest.mark.parametrize('argv', [
    ['gunicorn'],
    ['manage.py', 'load_athena_vocabularies'],
    ['manage.py', 'setup_admin'],
    ['manage.py', 'check', '--deploy', '--fail-level', 'ERROR'],
])
def test_render_boots_without_dashboard_allowed_hosts(argv):
    result = boot(argv, RENDER_EXTERNAL_HOSTNAME='service.example.invalid')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ['service.example.invalid']


@pytest.mark.parametrize('render_host', ['', 'service.example.invalid', 'app.example.invalid'])
def test_custom_domains_are_preserved_and_render_host_is_deduplicated(render_host):
    result = boot(
        ['gunicorn'], ALLOWED_HOSTS=' app.example.invalid, api.example.invalid, ',
        RENDER_EXTERNAL_HOSTNAME=render_host,
    )
    assert result.returncode == 0, result.stderr
    expected = ['app.example.invalid', 'api.example.invalid']
    if render_host and render_host not in expected:
        expected.append(render_host)
    assert json.loads(result.stdout) == expected


@pytest.mark.parametrize('hosts', ['', ' , '])
@pytest.mark.parametrize('argv', [['gunicorn'], ['manage.py', 'check', '--deploy']])
def test_production_requires_a_resolved_host(argv, hosts):
    result = boot(argv, ALLOWED_HOSTS=hosts)
    assert result.returncode != 0
    assert 'ALLOWED_HOSTS must be set' in result.stderr


@pytest.mark.parametrize('missing', ['SECRET_KEY', 'DATABASE_URL', 'CORS_ALLOWED_ORIGINS'])
def test_deploy_check_validates_other_runtime_requirements(missing):
    value = 'django-insecure-placeholder' if missing == 'SECRET_KEY' else ''
    result = boot(
        ['manage.py', 'check', '--deploy'],
        RENDER_EXTERNAL_HOSTNAME='service.example.invalid', **{missing: value},
    )
    assert result.returncode != 0
    assert f'{missing} must be set' in result.stderr


@pytest.mark.parametrize('command', ['collectstatic', 'check', 'migrate'])
def test_build_and_plain_management_checks_keep_their_exemption(command):
    result = boot(['manage.py', command])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
