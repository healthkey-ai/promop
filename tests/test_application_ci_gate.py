"""The ruleset gate must exempt docs without exempting failed code checks."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.github/scripts/application_ci_gate.py'
spec = importlib.util.spec_from_file_location('application_ci_gate', SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


@pytest.mark.parametrize('backend,frontend', [
    ('skipped', 'skipped'), ('success', 'success'), ('cancelled', 'cancelled'),
])
def test_documentation_needs_no_application_suite_results(backend, frontend):
    assert gate.application_ci_passes(changes='success', docs_only='true',
        backend_required='false', backend=backend, frontend=frontend)


@pytest.mark.parametrize('backend,frontend,expected', [
    ('success', 'success', True), ('failure', 'success', False),
    ('cancelled', 'success', False), ('skipped', 'success', False),
    ('success', 'failure', False), ('success', 'cancelled', False),
    ('success', 'skipped', False), ('', '', False),
])
def test_code_requires_successful_application_suites(backend, frontend, expected):
    assert gate.application_ci_passes(changes='success', docs_only='false',
        backend_required='true', backend=backend, frontend=frontend) is expected


def test_frontend_only_change_can_skip_backend():
    assert gate.application_ci_passes(changes='success', docs_only='false',
        backend_required='false', backend='skipped', frontend='success')


@pytest.mark.parametrize('changes', ['failure', 'cancelled', 'skipped', ''])
def test_failed_detection_cannot_claim_documentation_or_frontend_exemption(changes):
    assert not gate.application_ci_passes(changes=changes, docs_only='true',
        backend_required='false', backend='skipped', frontend='success')
    assert gate.application_ci_passes(changes=changes, docs_only='true',
        backend_required='false', backend='success', frontend='success')


def test_workflow_runs_gate_even_when_dependencies_are_skipped_or_fail():
    import yaml
    workflow = yaml.safe_load((SCRIPT.parents[2] / '.github/workflows/ci.yml').read_text())
    job = workflow['jobs']['application_ci']
    assert job['if'] == 'always()'
    assert set(job['needs']) == {'changes', 'backend', 'frontend'}
    assert job['steps'][-1]['run'] == 'python3 .github/scripts/application_ci_gate.py'
