"""Browser packaging must survive deployment and fail early when unusable."""
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from scripts.install_athena_browser import install

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('promop', 'promop-worker', 'promop-staging', 'promop-staging-worker')


@pytest.mark.parametrize('name', SERVICES)
@pytest.mark.parametrize('browser_state', ['working', 'broken'])
def test_actual_render_build_installs_browser_after_pip_and_propagates_failure(tmp_path, name, browser_state):
    service = next(s for s in yaml.safe_load((ROOT / 'render.yaml').read_text())['services'] if s['name'] == name)
    environment = {e['key']: e.get('value') for e in service['envVars']}
    assert environment['PLAYWRIGHT_BROWSERS_PATH'] == '0'
    (tmp_path / 'scripts').mkdir(); (tmp_path / 'frontend').mkdir(); (tmp_path / 'bin').mkdir()
    for filename in ('verify_source_catalog_snapshots.py', 'install_athena_browser.py'):
        (tmp_path / 'scripts' / filename).write_text('placeholder')
    stub = '''#!/bin/sh
printf '%s %s\\n' "${0##*/}" "$*" >> "$BUILD_TEST_LOG"
if [ "$1" = scripts/install_athena_browser.py ]; then
    [ "$PLAYWRIGHT_BROWSERS_PATH" = 0 ] || exit 8
    [ "$BROWSER_STATE" != broken ] || exit 9
fi
'''
    for executable in ('python', 'pip', 'npm'):
        path = tmp_path / 'bin' / executable; path.write_text(stub); path.chmod(0o755)
    log = tmp_path / 'build.log'
    result = subprocess.run(['/bin/sh', '-c', service['buildCommand']], cwd=tmp_path,
        env={**os.environ, 'PATH': str(tmp_path / 'bin')+':'+os.environ['PATH'],
             'PLAYWRIGHT_BROWSERS_PATH': environment['PLAYWRIGHT_BROWSERS_PATH'],
             'BUILD_TEST_LOG': str(log), 'BROWSER_STATE': browser_state}, capture_output=True, text=True)
    calls = log.read_text().splitlines()
    assert calls.index('pip install -r requirements.txt') < calls.index('python scripts/install_athena_browser.py')
    assert result.returncode == (0 if browser_state == 'working' else 9), result.stderr
    if browser_state == 'broken':
        assert calls[-1] == 'python scripts/install_athena_browser.py'


def test_python_dependency_is_pinned_and_legacy_install_remains_consistent():
    def pin(filename):
        return next(line for line in (ROOT / filename).read_text().splitlines() if line.startswith('playwright=='))
    assert pin('requirements.txt') == pin('requirements-athena-scrape.txt')


@pytest.mark.parametrize('with_deps', [False, True])
def test_installer_uses_runtime_location_and_checks_browser_in_fresh_process(monkeypatch, with_deps):
    monkeypatch.delenv('PLAYWRIGHT_BROWSERS_PATH', raising=False)
    with patch('scripts.install_athena_browser.subprocess.run') as run:
        install(with_deps=with_deps)
    first, second = run.call_args_list
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == '0'
    assert '--only-shell' in first.args[0] and 'chromium' in first.args[0]
    assert ('--with-deps' in first.args[0]) == with_deps
    assert second.args[0][1] == '-c'
    assert 'chromium.launch(headless=True)' in second.args[0][2]
    assert all(call.kwargs['check'] for call in run.call_args_list)


@pytest.mark.parametrize('failure_step', [0, 1])
def test_installer_propagates_download_and_launch_failures(monkeypatch, failure_step):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', '/custom/browser/location')
    failure = subprocess.CalledProcessError(7, ['playwright'])
    effects = [failure] if failure_step == 0 else [None, failure]
    with patch('scripts.install_athena_browser.subprocess.run', side_effect=effects) as run:
        with pytest.raises(subprocess.CalledProcessError): install()
    assert run.call_count == failure_step + 1
    assert os.environ['PLAYWRIGHT_BROWSERS_PATH'] == '/custom/browser/location'


def test_docker_final_images_include_browser_and_linux_libraries():
    docker = (ROOT / 'Dockerfile').read_text()
    assert 'ENV PLAYWRIGHT_BROWSERS_PATH=0' in docker
    assert 'COPY scripts/install_athena_browser.py ./scripts/' in docker
    assert 'python scripts/install_athena_browser.py --with-deps' in docker
    final = (ROOT / 'Dockerfile.gcp').read_text().rsplit('FROM ', 1)[1]
    assert 'ENV PLAYWRIGHT_BROWSERS_PATH=0' in final
    assert final.index('COPY --from=backend /usr/local/lib/python3.12/site-packages') < final.index('python scripts/install_athena_browser.py --with-deps')
    assert final.index('COPY --from=backend /app /app') < final.index('python scripts/install_athena_browser.py --with-deps')
