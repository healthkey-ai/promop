"""Athena browser tooling stays explicit and out of app deployments."""
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from scripts.install_athena_browser import install, main

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ('promop', 'promop-worker', 'promop-staging', 'promop-staging-worker')


@pytest.mark.parametrize('name', SERVICES)
def test_render_application_builds_do_not_install_browser(name):
    service = next(s for s in yaml.safe_load((ROOT / 'render.yaml').read_text())['services'] if s['name'] == name)
    environment = {e['key']: e.get('value') for e in service['envVars']}
    assert 'PLAYWRIGHT_BROWSERS_PATH' not in environment
    assert 'install_athena_browser.py' not in service['buildCommand']


def test_python_dependency_is_only_in_optional_browser_requirements():
    main = (ROOT / 'requirements.txt').read_text().splitlines()
    optional = (ROOT / 'requirements-athena-scrape.txt').read_text().splitlines()
    assert not any(line.startswith('playwright') for line in main)
    assert any(line.startswith('playwright==') for line in optional)


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


def test_stale_deploy_command_skips_when_optional_package_is_absent(capsys):
    with patch('scripts.install_athena_browser.importlib.util.find_spec', return_value=None), \
            patch('scripts.install_athena_browser.install') as install_browser:
        main([])
    install_browser.assert_not_called()
    assert 'skipping optional Athena Chromium setup' in capsys.readouterr().out


def test_explicit_operator_install_fails_when_optional_package_is_absent(capsys):
    with patch('scripts.install_athena_browser.importlib.util.find_spec', return_value=None), \
            pytest.raises(SystemExit) as exc:
        main(['--require-package'])
    assert exc.value.code == 2
    assert 'Install requirements-athena-scrape.txt first' in capsys.readouterr().err


@pytest.mark.parametrize('filename', ['Dockerfile', 'Dockerfile.gcp'])
def test_default_docker_images_do_not_install_browser(filename):
    docker = (ROOT / filename).read_text()
    assert 'PLAYWRIGHT_BROWSERS_PATH' not in docker
    assert 'python scripts/install_athena_browser.py' not in docker
