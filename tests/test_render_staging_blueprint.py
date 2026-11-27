"""Deployment wiring must not replace staging data or split broker identities."""
import os
from pathlib import Path
import subprocess

import pytest
import yaml

from promop.frontend_paths import resolve_frontend_root

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
    # Staging itself, and the phr installation it hosts identity for (phr#43).
    assert web_env['CORS_ALLOWED_ORIGINS']['value'] == (
        'https://promop-staging.onrender.com,https://phr-7kf7.onrender.com'
    )
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


def test_worker_start_defaults_to_one_child(tmp_path):
    celery = tmp_path / 'celery'
    celery.write_text('#!/bin/bash\nprintf "%s %s %s" "$CELERY_WORKER_CONCURRENCY" "$CELERY_WORKER_PREFETCH_MULTIPLIER" "$*"\n')
    celery.chmod(0o755)
    env = {'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'CELERY_BROKER_URL': 'redis://example.invalid',
           'DATABASE_URL': 'postgresql://example.invalid', 'SECRET_KEY': 'test-only'}
    result = subprocess.run(['bash', str(ROOT / 'start-worker.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '1 1 -A promop worker --loglevel=info --queues=celery,athena,suggest'


def test_staging_hosts_identity_for_phr_and_production_does_not_yet():
    """Family auth (phr#43): staging issues phr's tokens; production is untouched."""
    result = yaml.safe_load((ROOT / 'render.yaml').read_text())
    by_name = {s['name']: s for s in result['services']}
    staging = {e['key']: e for e in by_name['promop-staging']['envVars']}
    production = {e['key']: e for e in by_name['promop']['envVars']}

    # The signing key is a secret entered once, never a value in the repo.
    assert staging['FAMILY_AUTH_PRIVATE_KEY'] == {'key': 'FAMILY_AUTH_PRIVATE_KEY', 'sync': False}
    assert staging['FAMILY_AUTH_ISSUER']['value'] == 'https://promop-staging.onrender.com'
    # PROMOP's own audience first, then each sibling that checks `aud` (exact#700).
    assert staging['FAMILY_AUTH_AUDIENCE']['value'] == 'promop-api,exact-api,soc-api,labs-api'
    assert staging['PHR_AUDIENCE']['value'] == 'promop-api'
    assert staging['FAMILY_AUTH_SIGNUP_ORGS']['value'] == 'phr:HealthKey PHR'
    # phr's sign-ups verify on phr, at the origin CORS lets call the API.
    app_slug, app_url = staging['FAMILY_AUTH_APP_URLS']['value'].split('=')
    assert app_slug == 'phr'
    assert app_url in staging['CORS_ALLOWED_ORIGINS']['value'].split(',')
    assert not any(key.startswith('FAMILY_AUTH_') for key in production)



def test_staging_build_publishes_the_federation_remote(tmp_path):
    """phr loads promop's patient record from /remote/remoteEntry.js. The
    Dockerfile builds it into frontend/build/remote; the staging Blueprint must
    too, or WhiteNoise has no such file and the SPA fallback answers with HTML.
    It must also keep frontend/build the served root (see below)."""
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    command = next(s for s in config['services'] if s['name'] == 'promop-staging')['buildCommand']
    (tmp_path / 'frontend').mkdir()
    # The Python install is its own script (scripts/install-python-deps.sh,
    # tested below); here it only has to exist and succeed.
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'install-python-deps.sh').write_text('#!/bin/sh\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    # npm stands in for both Vite builds: each writes its output where the
    # real config puts it (build/ for the SPA, dist/remote/ for the remote).
    stubs = {
        'python': '#!/bin/sh\n',
        'pip': '#!/bin/sh\n',
        'npm': '''#!/bin/sh
case "$*" in
  "run build") mkdir -p build && echo spa > build/index.html ;;
  "run build:remote") mkdir -p dist/remote && echo remote > dist/remote/remoteEntry.js ;;
esac
''',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    result = subprocess.run(['/bin/sh', '-c', command], cwd=tmp_path, env=env, capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    # Render sets no WHITENOISE_ROOT, so the served root is whichever build
    # directory resolve_frontend_root finds first. A dist/remote left behind
    # wins over build/ and moves the remote to /remoteEntry.js.
    served = resolve_frontend_root(tmp_path)
    assert served == tmp_path / 'frontend' / 'build'
    assert (served / 'index.html').read_text() == 'spa\n'
    assert (served / 'remote' / 'remoteEntry.js').read_text() == 'remote\n'


def _service(name):
    config = yaml.safe_load((ROOT / 'render.yaml').read_text())
    return next(s for s in config['services'] if s['name'] == name)


def test_staging_installs_cpu_only_torch_through_the_script():
    """The default torch wheel brings ~3 GB of CUDA libraries a GPU-less Render
    instance never loads; every staging build goes through the script that
    installs the CPU wheel first (#2937)."""
    script = ROOT / 'scripts' / 'install-python-deps.sh'
    assert script.exists()
    body = script.read_text()
    assert 'download.pytorch.org/whl/cpu' in body
    assert 'pip install -r requirements.txt' in body
    for name in ('promop-staging', 'promop-staging-worker'):
        command = _service(name)['buildCommand']
        assert 'bash scripts/install-python-deps.sh' in command
        assert 'pip install -r requirements.txt' not in command


def test_only_staging_web_skips_the_second_prepare_on_boot():
    """Staging runs prepare-deployment.sh as its preDeployCommand, so start.sh
    need not repeat it. Production has no pre-deploy command: start.sh must keep
    preparing there (#2937)."""
    def env(service):
        return {e['key']: e.get('value') for e in service['envVars']}

    staging = _service('promop-staging')
    assert staging['preDeployCommand'] == 'bash scripts/prepare-deployment.sh'
    assert env(staging)['SKIP_PREPARE_ON_BOOT'] == 'true'
    production = _service('promop')
    assert 'preDeployCommand' not in production
    assert 'SKIP_PREPARE_ON_BOOT' not in env(production)


@pytest.mark.parametrize('skip, prepared', [('true', False), ('', True), ('false', True)])
def test_start_sh_prepares_on_boot_unless_told_it_was_done(tmp_path, skip, prepared):
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'prepare-deployment.sh').write_text('touch prepared\n')
    (tmp_path / 'start.sh').write_text((ROOT / 'start.sh').read_text())
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'gunicorn').write_text('#!/bin/sh\ntouch served\n')
    (bin_dir / 'gunicorn').chmod(0o755)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'SKIP_PREPARE_ON_BOOT': skip}
    result = subprocess.run(['bash', 'start.sh'], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'served').exists()
    assert (tmp_path / 'prepared').exists() is prepared


@pytest.mark.parametrize('name', ['promop', 'promop-worker'])
def test_production_builds_cpu_torch_only_when_the_revision_has_the_script(tmp_path, name):
    """This shared Blueprint can be synced before main has the script (#1625 hit
    the same trap with scripts/prepare-deployment.sh), so the production build
    falls back to the plain pip install when it is absent."""
    command = _service(name)['buildCommand']
    assert 'if [ -f scripts/install-python-deps.sh ]' in command
    log = tmp_path / 'log'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for tool in ('python', 'pip', 'npm'):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho {tool} "$@" >> {log}\n')
        (bin_dir / tool).chmod(0o755)
    (tmp_path / 'frontend').mkdir()
    # The part of the command up to the frontend build is the Python install.
    install = command.split(' && cd frontend')[0]
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}

    without = subprocess.run(['/bin/sh', '-c', install], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert without.returncode == 0, without.stderr
    assert 'pip install -r requirements.txt' in log.read_text()

    log.write_text('')
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts' / 'install-python-deps.sh').write_text(f'echo script >> {log}\n')
    with_script = subprocess.run(['/bin/sh', '-c', install], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert with_script.returncode == 0, with_script.stderr
    assert log.read_text().strip().endswith('script')
    assert 'pip install' not in log.read_text()
