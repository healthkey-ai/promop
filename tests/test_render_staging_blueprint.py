"""Deployment wiring must not replace staging data or split broker identities."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_staging_reuses_existing_web_settings_and_shares_broker():
    result = yaml.safe_load((ROOT / 'render.yaml').read_text())
    web, worker, broker = [s for s in result['services'] if s['name'].startswith('promop-staging')]
    assert web['name'] == 'promop-staging'
    assert all(d['name'] != 'ctomop_dev' for d in result.get('databases', []))
    assert web['branch'] == worker['branch'] == 'dev'
    assert {s['region'] for s in (web, worker, broker)} == {'oregon'}
    web_env = {e['key']: e for e in web['envVars']}
    worker_env = {e['key']: e for e in worker['envVars']}
    for key in ('DATABASE_URL', 'SECRET_KEY', 'AUDIT_HMAC_KEY', 'EXPORT_SIGNING_KEY'):
        expected = {'sync': False} if key == 'DATABASE_URL' else {'generateValue': True}
        assert web_env[key] == {'key': key, **expected}
        assert worker_env[key]['fromService'] == {
            'name': web['name'], 'type': 'web', 'envVarKey': key,
        }
    assert web_env['CELERY_BROKER_URL'] == worker_env['CELERY_BROKER_URL']
    assert web_env['CELERY_RESULT_BACKEND'] == worker_env['CELERY_RESULT_BACKEND']
    assert web_env['DEBUG']['value'] == 'False'
    assert web_env['ALLOWED_HOSTS']['value'] == 'promop-staging.onrender.com'
    assert web_env['CORS_ALLOWED_ORIGINS']['value'] == 'https://promop-staging.onrender.com'
    assert web_env['APP_BASE_URL']['value'] == 'https://promop-staging.onrender.com'
    assert web_env['SERVICE_AUTH_SCOPES']['value'] == 'patient/*.read system/etl.write'
    assert 'patient/*.write' not in web_env['SERVICE_AUTH_SCOPES']['value'].split()
    assert web_env['CELERY_BROKER_URL']['fromService']['name'] == broker['name']
    assert worker_env['CELERY_WORKER_CONCURRENCY']['value'] == '1'
    assert broker['maxmemoryPolicy'] == 'noeviction'
    assert broker['ipAllowList'] == []


def test_production_yaml_keeps_http_settings_on_web_service():
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    web = next(s for s in config['services'] if s['type'] == 'web')
    assert web['branch'] == 'main'
    env = {e['key']: e for e in web['envVars']}
    assert env['CELERY_BROKER_URL'] == {'key': 'CELERY_BROKER_URL', 'sync': False}
    assert {'ALLOWED_HOSTS', 'CORS_ALLOWED_ORIGINS', 'ADMIN_EMAIL'} <= env.keys()
    assert 'SERVICE_AUTH_SCOPES' not in env


@pytest.mark.parametrize('missing', ['CELERY_BROKER_URL', 'DATABASE_URL', 'SECRET_KEY'])
def test_worker_refuses_missing_required_configuration(missing):
    env = {'PATH': os.environ['PATH'], 'CELERY_BROKER_URL': 'redis://example.invalid',
           'DATABASE_URL': 'postgresql://example.invalid', 'SECRET_KEY': 'test-only'}
    del env[missing]
    result = subprocess.run(['bash', str(ROOT / 'start-worker.sh')], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert missing in result.stderr


def test_worker_start_defaults_to_one_child(tmp_path):
    celery = tmp_path / 'celery'
    celery.write_text('#!/bin/bash\nprintf "%s %s %s" "$CELERY_WORKER_CONCURRENCY" "$CELERY_WORKER_PREFETCH_MULTIPLIER" "$*"\n')
    celery.chmod(0o755)
    env = {'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'CELERY_BROKER_URL': 'redis://example.invalid',
           'DATABASE_URL': 'postgresql://example.invalid', 'SECRET_KEY': 'test-only'}
    result = subprocess.run(['bash', str(ROOT / 'start-worker.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '1 1 -A promop worker --loglevel=info'
