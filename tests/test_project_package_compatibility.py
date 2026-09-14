"""Old deployment/import paths share canonical apps and integration contracts."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('first_package', ['promop', 'ctomop'])
def test_both_import_orders_share_settings_routes_and_applications(first_package):
    env = {**os.environ, 'DJANGO_SETTINGS_MODULE': first_package + '.settings',
           'DEBUG': 'True', 'SECRET_KEY': 'local-project-compatibility-test',
           'DATABASE_URL': 'postgresql://postgres@localhost:5432/unused_bootstrap_test',
           'PGOPTIONS': '-c default_transaction_read_only=on',
           'RENDER': '', 'RENDER_SERVICE_ID': '', 'K_SERVICE': '',
           'SENTRY_DSN': '', 'CELERY_BROKER_URL': 'memory://',
           'CELERY_RESULT_BACKEND': 'cache+memory://'}
    script = '''
import importlib
import sys
import django
from celery.app.utils import find_app
first, second = sys.argv[1:]
importlib.import_module(first)
django.setup()
for suffix in ('settings', 'urls', 'celery', 'wsgi', 'asgi', 'sentry', 'frontend_paths', 'whitenoise'):
    before = importlib.import_module(first + '.' + suffix)
    after = importlib.import_module(second + '.' + suffix)
    assert before is after, suffix
from django.conf import settings
from django.apps import apps
from django.urls import resolve
from django.db.migrations.loader import MigrationLoader
assert settings.ROOT_URLCONF == 'promop.urls'
assert settings.WSGI_APPLICATION == 'promop.wsgi.application'
assert 'promop' not in apps.app_configs and 'ctomop' not in apps.app_configs
loader = MigrationLoader(None)
assert not any(label in ('promop', 'ctomop') for label, _ in loader.disk_migrations)
assert resolve('/o/authorize/').url_name == 'authorize'
assert resolve('/o/token/').url_name == 'token'
assert resolve('/api/v1/health/').url_name == 'health_check_v1'
assert resolve('/api/health/').url_name == 'health_check'
app = find_app(first)
assert app is find_app(second)
app.autodiscover_tasks(force=True)
assert 'omop_core.refresh_patient_record' in app.tasks
assert 'omop_core.suggest_mappings' in app.tasks
assert app.conf.task_default_queue == 'celery'
# A legacy monkeypatch must affect the actual canonical module globals too.
legacy = importlib.import_module('ctomop.sentry')
canonical = importlib.import_module('promop.sentry')
sentinel = object()
legacy._compatibility_probe = sentinel
assert canonical._compatibility_probe is sentinel
print('compatible')
'''
    other = 'ctomop' if first_package == 'promop' else 'promop'
    result = subprocess.run([sys.executable, '-c', script, first_package, other],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'compatible'


def test_cloud_run_and_external_identity_contracts_are_preserved():
    workflow = (ROOT / '.github/workflows/deploy-staging.yml').read_text()
    assert 'branches: [dev]' in workflow
    assert 'SERVICE_NAME: ctomop-staging' in workflow
    assert '${ARTIFACT_REPO}/ctomop:${IMAGE_TAG}' in workflow
    assert '--to-latest' in workflow
    dockerfile = (ROOT / 'Dockerfile.gcp').read_text()
    assert 'promop.wsgi:application' in dockerfile
    assert 'npm run build:remote' in dockerfile
    oauth = (ROOT / 'frontend/src/utils/oauth.ts').read_text()
    assert "VITE_OAUTH_CLIENT_ID ?? 'ctomop-smart-app'" in oauth
    bridge = (ROOT / 'docker-compose.bridge.yml').read_text()
    assert '${CTOMOP_SERVICE_TOKEN:-local-bridge-service-token}' in bridge
