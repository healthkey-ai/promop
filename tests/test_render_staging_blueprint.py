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


@pytest.mark.parametrize('service_name', ['promop', 'promop-worker'])
@pytest.mark.parametrize('verifier_state', ['absent', 'valid', 'invalid'])
def test_production_build_supports_older_main_and_enforces_present_verifier(
    tmp_path, service_name, verifier_state,
):
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    service = next(s for s in config['services'] if s['name'] == service_name)
    command = service['buildCommand']
    (tmp_path / 'frontend').mkdir()
    if verifier_state != 'absent':
        (tmp_path / 'scripts').mkdir()
        (tmp_path / 'scripts' / 'verify_source_catalog_snapshots.py').write_text('placeholder')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    # Exercise the actual Blueprint shell command without installing packages.
    # The Python stub mimics both the reported missing-file error and a failed
    # checksum check, so neither can be hidden by the command's conditionals.
    stub = '''#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$BUILD_TEST_LOG"
if [ "${0##*/}" = python ] && [ "$1" = scripts/verify_source_catalog_snapshots.py ]; then
    [ -f "$1" ] || exit 2
    [ "$BUILD_TEST_VERIFIER" != invalid ] || exit 7
fi
'''
    for name in ('python', 'pip', 'npm'):
        path = bin_dir / name
        path.write_text(stub)
        path.chmod(0o755)
    log = tmp_path / 'commands.log'
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}',
           'BUILD_TEST_LOG': str(log), 'BUILD_TEST_VERIFIER': verifier_state}
    result = subprocess.run(['/bin/sh', '-c', command], cwd=tmp_path, env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines()
    if verifier_state == 'invalid':
        assert result.returncode == 7
        assert calls == ['python scripts/verify_source_catalog_snapshots.py']
    else:
        assert result.returncode == 0, result.stderr
        assert 'pip install -r requirements.txt' in calls
        assert ('python scripts/verify_source_catalog_snapshots.py' in calls) == (verifier_state == 'valid')
        if service_name == 'promop':
            assert calls[-1] == 'python manage.py collectstatic --noinput'


@pytest.mark.parametrize('missing', ['CELERY_BROKER_URL', 'DATABASE_URL', 'SECRET_KEY'])
def test_worker_refuses_missing_required_configuration(missing):
    env = {'PATH': os.environ['PATH'], 'CELERY_BROKER_URL': 'redis://example.invalid',
           'DATABASE_URL': 'postgresql://example.invalid', 'SECRET_KEY': 'test-only'}
    del env[missing]
    result = subprocess.run(['bash', str(ROOT / 'start-worker.sh')], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert missing in result.stderr


@pytest.mark.parametrize('embedded_beat', ['true', 'false'])
def test_worker_start_defaults_to_one_child(tmp_path, embedded_beat):
    celery = tmp_path / 'celery'
    celery.write_text('#!/bin/bash\nprintf "%s %s %s" "$CELERY_WORKER_CONCURRENCY" "$CELERY_WORKER_PREFETCH_MULTIPLIER" "$*"\n')
    celery.chmod(0o755)
    env = {'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'CELERY_BROKER_URL': 'redis://example.invalid',
           'DATABASE_URL': 'postgresql://example.invalid', 'SECRET_KEY': 'test-only',
           'CELERY_EMBEDDED_BEAT': embedded_beat}
    result = subprocess.run(['bash', str(ROOT / 'start-worker.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    expected = '1 1 -A promop worker --loglevel=info'
    if embedded_beat == 'true':
        expected += ' --beat --schedule=/tmp/promop-celerybeat-schedule'
    assert result.stdout == expected


def test_production_worker_runs_the_entrypoint_that_can_schedule_recovery():
    """Without beat the recovery sweep and retention never run in production."""
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    worker = next(s for s in config['services']
                  if s['type'] == 'worker' and s['name'] == 'promop-worker')
    assert worker['branch'] == 'main'
    assert worker['startCommand'] == 'bash start-worker.sh'
    env = {e['key']: e.get('value') for e in worker['envVars']}
    assert env['CELERY_EMBEDDED_BEAT'] == 'true'
    # Pinned so moving to start-worker.sh does not cut concurrency to its default.
    assert env['CELERY_WORKER_CONCURRENCY'] == '4'


def test_the_webhook_flag_is_declared_once_and_pulled_by_the_worker():
    """Both beat tasks no-op unless WEBHOOKS_ENABLED is true, so a worker whose
    flag differs from its web service is a silent stranded-delivery machine."""
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    for web_name, worker_name in (('promop', 'promop-worker'),
                                  ('promop-staging', 'promop-staging-worker')):
        web = next(s for s in config['services'] if s['name'] == web_name)
        worker = next(s for s in config['services'] if s['name'] == worker_name)
        web_env = {e['key']: e for e in web['envVars']}
        worker_env = {e['key']: e for e in worker['envVars']}
        # Operator-controlled on the web service, so there is one place to flip.
        assert web_env['WEBHOOKS_ENABLED'] == {'key': 'WEBHOOKS_ENABLED', 'sync': False}
        assert worker_env['WEBHOOKS_ENABLED']['fromService'] == {
            'name': web_name, 'type': 'web', 'envVarKey': 'WEBHOOKS_ENABLED',
        }


def test_exactly_one_scheduler_across_the_blueprint():
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    with_beat = [
        s['name'] for s in config['services'] if s['type'] == 'worker'
        and any(e['key'] == 'CELERY_EMBEDDED_BEAT' and e.get('value') == 'true'
                for e in s['envVars'])
    ]
    # One per deployment, not one per blueprint: production and staging are
    # separate brokers and databases.
    assert sorted(with_beat) == ['promop-staging-worker', 'promop-worker']
    for name in with_beat:
        service = next(s for s in config['services'] if s['name'] == name)
        assert service['startCommand'] == 'bash start-worker.sh'
        # Configuration, not a comment: N replicas would be N schedulers, each
        # queueing the recovery sweep every minute and a daily prune.
        assert service['numInstances'] == 1
