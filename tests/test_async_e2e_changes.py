"""Guard the CI skip boundary and its PR merge-base comparison."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "async_e2e_changes", ROOT / ".github/scripts/async_e2e_changes.py",
)
filter_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(filter_module)


@pytest.mark.parametrize("path", [
    "frontend/src/federation/PatientInfoBridge.tsx",
    "frontend/package-lock.json", "docs/async-derivation-celery-plan.md",
    "README.md", "docker-compose.bridge.yml",
    "omop_core/services/patient_record_service.py", "omop_core/data/catalog.json",
    "patient_portal/api/serializers.py", "new_backend/config.toml",
])
def test_browser_and_documentation_changes_skip(path):
    assert not filter_module.requires_async_e2e([path])


@pytest.mark.parametrize("path", [
    "omop_core/tasks.py", "another_app/tasks/email.py", "promop/celery.py",
    "promop/__init__.py", "ctomop/__init__.py", "ctomop/settings.py",
    "ctomop/celery.py", "omop_core/services/derivation_jobs.py",
    "omop_core/services/suggest_jobs.py", "omop_core/services/embedding_jobs.py",
    "tests/test_celery_e2e.py", "start-worker.sh",
    ".github/workflows/ci.yml", ".github/scripts/async_e2e_changes.py",
])
def test_async_changes_run_even_with_frontend_changes(path):
    assert filter_module.requires_async_e2e(["frontend/package.json", path])


def test_merge_base_ignores_base_only_changes_and_detects_backend_rename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI Test")
    git("config", "user.email", "ci@example.test")
    (tmp_path / "omop_core").mkdir()
    (tmp_path / "omop_core/tasks.py").write_text("# async task\n")
    git("add", ".")
    git("commit", "-qm", "common base")
    common = git("rev-parse", "HEAD")
    (tmp_path / "requirements.txt").write_text("dependency\n")
    git("add", ".")
    git("commit", "-qm", "base-only change")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "--detach", common)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/page.tsx").write_text("// UI\n")
    git("add", ".")
    git("commit", "-qm", "frontend PR")
    assert not filter_module.requires_async_e2e(filter_module.changed_paths(base, "HEAD"))
    assert not filter_module.select_range(base, "HEAD")
    assert filter_module.select_checks(base, "HEAD")[2] is False
    before_push = git("rev-parse", "HEAD")
    git("mv", "omop_core/tasks.py", "frontend/tasks.py")
    git("commit", "-qm", "move backend to ignored path")
    assert filter_module.requires_async_e2e(filter_module.changed_paths(base, "HEAD"))
    (tmp_path / "frontend/page.tsx").write_text("// another UI change\n")
    git("add", ".")
    git("commit", "-qm", "last commit of multi-commit push is frontend-only")
    assert not filter_module.requires_async_e2e(
        filter_module.changed_paths("HEAD^", "HEAD", merge_base=False))
    assert filter_module.requires_async_e2e(
        filter_module.changed_paths(before_push, "HEAD", merge_base=False))
    assert filter_module.select_range(before_push, "HEAD", merge_base=False)
    assert filter_module.select_checks(before_push, "HEAD", merge_base=False)[2] is True


@pytest.mark.parametrize("event_name,paths,expected", [
    ("pull_request", ["frontend/page.tsx"], "false"),
    ("push", ["frontend/page.tsx"], "false"),
    ("push", ["docs/guide.md"], "false"),
    ("push", ["promop/celery.py"], "true"),
    ("workflow_dispatch", [], "true"),
])
def test_event_selection_and_job_output(tmp_path, monkeypatch, event_name, paths, expected):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({
        "pull_request": {"base": {"sha": "base"}, "head": {"sha": "head"}},
        "before": "before-push", "after": "after-push",
    }))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", event_name)
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    def select_checks(base, head, *, merge_base=True):
        if event_name == "push":
            assert (base, head, merge_base) == ("before-push", "after-push", False)
        else:
            assert (base, head, merge_base) == ("base", "head", True)
        return (
            filter_module.requires_async_e2e(paths),
            filter_module.is_docs_only(paths),
            filter_module.requires_backend(paths),
            filter_module.requires_browser_runtime(paths),
        )

    monkeypatch.setattr(filter_module, "select_checks", select_checks)
    filter_module.main()
    docs_only = event_name in {"pull_request", "push"} and filter_module.is_docs_only(paths)
    backend = event_name not in {"pull_request", "push"} or filter_module.requires_backend(paths)
    browser_runtime = (
        event_name not in {"pull_request", "push"}
        or filter_module.requires_browser_runtime(paths)
    )
    assert output.read_text() == (
        f"async_e2e={expected}\n"
        f"docs_only={str(docs_only).lower()}\n"
        f"backend={str(backend).lower()}\n"
        f"browser_runtime={str(browser_runtime).lower()}\n"
    )


def test_new_branch_push_runs_without_a_comparison(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": "0" * 40, "after": "new-head"}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    filter_module.main()
    assert output.read_text() == (
        "async_e2e=true\ndocs_only=false\nbackend=true\nbrowser_runtime=true\n"
    )


@pytest.mark.parametrize("path,before,after,expected", [
    ("patient_portal/api/views.py", "def profile(): return 1", "def profile(): return 2", False),
    ("patient_portal/api/views.py", "def derivation_status(): return 1", "def derivation_status(): return 2", True),
    ("patient_portal/api/views.py", "class PatientRecordV1ViewSet:\n def refresh(self): return 1",
     "class PatientRecordV1ViewSet:\n def refresh(self): return 2", True),
    ("patient_portal/api/views.py", "def code_mapping_suggest_run(): return 1",
     "def code_mapping_suggest_run(): return 2", True),
    ("promop/settings.py", "DEBUG = True", "DEBUG = False", False),
    ("promop/settings.py", "CELERY_BROKER_TRANSPORT_OPTIONS = {\n 'visibility_timeout': 100\n}",
     "CELERY_BROKER_TRANSPORT_OPTIONS = {\n 'visibility_timeout': 200\n}", True),
    ("requirements.txt", "celery==5.5.3\nDjango==5.2.1", "celery==5.5.3\nDjango==5.2.2", False),
    ("requirements.txt", "celery==5.5.3", "celery==5.5.4", True),
    ("new_backend/dispatch.py", "", "task.apply_async(args=[1])", True),
    ("render.yaml", "  - key: CELERY_WORKER_CONCURRENCY\n    value: '1'",
     "  - key: CELERY_WORKER_CONCURRENCY\n    value: '2'", True),
    ("render.yaml", "  - type: worker\n    plan: small",
     "  - type: worker\n    plan: large", True),
    ("render.yaml", "  - type: web\n    plan: small",
     "  - type: web\n    plan: large", False),
    ("omop_core/models.py", "class Person: pass\nclass SuggestRun: state = 1",
     "class Person: name = ''\nclass SuggestRun: state = 1", False),
    ("omop_core/models.py", "class SuggestRun: state = 1", "class SuggestRun: state = 2", True),
])
def test_shared_files_only_run_for_async_changes(monkeypatch, path, before, after, expected):
    monkeypatch.setattr(filter_module, "file_at", lambda rev, path: before if rev == "base" else after)
    assert filter_module.requires_async_e2e([path], "base", "head") is expected


@pytest.mark.parametrize("paths", [
    ["README.md", "docs/guide.md", "AGENTS.md", "CLAUDE.md"],
    ["docs/field_concept_mapping_plan.md", "docs/field_concept_mapping_architecture.md"],
    ["docs/utah-rhtp-technical-architecture-brief.md", "docs/utah-rhtp-brief.pdf"],
    ["docs/diagram.svg", "docs/screenshot.png", "docs/adr/evidence.txt"],
    [".github/PULL_REQUEST_TEMPLATE.md", "LICENSE"],
])
def test_prose_and_documentation_assets_skip_application_ci(paths):
    assert filter_module.is_docs_only(paths)


@pytest.mark.parametrize("path", [
    "omop_core/models.py", "tests/test_models.py", "frontend/src/page.tsx",
    "requirements.txt", "runtime.txt", "frontend/package-lock.json",
    "render.yaml", ".github/workflows/ci.yml", ".github/scripts/async_e2e_changes.py",
    "docs/ht-code-concept-mapping.md", "docs/code-concept-mappings.md",
    "docs/ht-fhir-code-concept-mapping.md", "docs/seed.json", "docs/helper.py",
    "data/prompt.md", "unknown-file", "Dockerfile",
])
def test_mixed_changes_and_runtime_documents_require_application_ci(path):
    assert not filter_module.is_docs_only(["README.md", path])


def test_empty_diff_requires_application_ci():
    assert not filter_module.is_docs_only([])


def test_docs_diff_uses_merge_base_and_includes_deleted_code_on_rename(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI Test")
    git("config", "user.email", "ci@example.test")
    (tmp_path / "app.py").write_text("print('source')\n")
    git("add", ".")
    git("commit", "-qm", "common")
    common = git("rev-parse", "HEAD")
    (tmp_path / "requirements.txt").write_text("dependency\n")
    git("add", ".")
    git("commit", "-qm", "base only")
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "--detach", common)
    (tmp_path / "README.md").write_text("Guide\n")
    git("add", ".")
    git("commit", "-qm", "docs PR")
    assert filter_module.select_checks(base, "HEAD") == (False, True, False, False)
    before_push = git("rev-parse", "HEAD")
    (tmp_path / "docs").mkdir()
    git("mv", "app.py", "docs/example.md")
    git("commit", "-qm", "rename code into docs")
    assert filter_module.select_checks(base, "HEAD")[1] is False
    (tmp_path / "README.md").write_text("Updated guide\n")
    git("add", ".")
    git("commit", "-qm", "last commit only changes prose")
    assert filter_module.select_checks("HEAD^", "HEAD", merge_base=False)[1] is True
    assert filter_module.select_checks(before_push, "HEAD", merge_base=False)[1] is False


def test_failed_diff_does_not_emit_a_docs_skip(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({
        "pull_request": {"base": {"sha": "missing-base"}, "head": {"sha": "missing-head"}},
    }))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    def unavailable(*args, **kwargs):
        raise subprocess.CalledProcessError(128, "git diff")

    monkeypatch.setattr(filter_module, "select_checks", unavailable)
    with pytest.raises(subprocess.CalledProcessError):
        filter_module.main()
    assert not output.exists()


@pytest.mark.parametrize('paths', [
    ['frontend/src/App.tsx'],
    ['frontend/package-lock.json', 'frontend/vite.config.ts', 'README.md'],
    ['frontend/src/assets/logo.svg', 'frontend/.npmrc'],
    ['docs/guide.md'],
])
def test_frontend_and_docs_changes_skip_backend(paths):
    assert not filter_module.requires_backend(paths)


@pytest.mark.parametrize('path', [
    'omop_core/models.py', 'patient_portal/api/views.py', 'tests/test_api.py',
    'requirements.txt', 'conftest.py', 'pytest.ini', 'render.yaml', 'Dockerfile',
    '.github/workflows/ci.yml', '.github/scripts/async_e2e_changes.py',
    'docs/ht-code-concept-mapping.md', 'frontend/backend_helper.py',
    'new_backend/config.toml', 'unknown-file',
])
def test_backend_shared_unknown_and_mixed_changes_keep_backend(path):
    assert filter_module.requires_backend(['frontend/src/App.tsx', path])


def test_empty_diff_keeps_backend():
    assert filter_module.requires_backend([])


@pytest.mark.parametrize('path', [
    'requirements-athena-scrape.txt',
    'scripts/install_athena_browser.py', 'tests/test_athena_browser_build.py',
    '.github/workflows/ci.yml', '.github/scripts/async_e2e_changes.py',
])
def test_browser_packaging_changes_run_runtime_smoke(path):
    assert filter_module.requires_browser_runtime([path])


@pytest.mark.parametrize('path', [
    'Dockerfile', 'Dockerfile.gcp', 'render.yaml', 'runtime.txt',
    'requirements.txt',
])
def test_application_packaging_changes_do_not_run_browser_smoke(path):
    assert not filter_module.requires_browser_runtime([path])


@pytest.mark.parametrize('paths', [
    ['omop_core/models.py'],
    ['tests/test_models.py', 'patient_portal/api/views.py'],
    ['frontend/src/App.tsx'],
    ['docs/guide.md'],
])
def test_ordinary_changes_skip_browser_runtime_smoke(paths):
    assert not filter_module.requires_browser_runtime(paths)


def test_unknown_browser_diff_runs_runtime_smoke():
    assert filter_module.requires_browser_runtime([])


@pytest.mark.parametrize('backend_result,browser_required,browser_result,passes', [
    ('success', 'false', 'skipped', True),
    ('success', 'true', 'success', True),
    ('success', 'true', 'failure', False),
    ('success', 'false', 'failure', False),
    ('failure', 'false', 'skipped', False),
    ('cancelled', 'false', 'skipped', False),
    ('skipped', 'false', 'skipped', False),
    ('', 'false', 'skipped', False),
])
def test_required_backend_gate_rejects_incomplete_matrix(
        backend_result, browser_required, browser_result, passes):
    import os
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    assert workflow['permissions'] == {'contents': 'read'}
    jobs = workflow['jobs']
    gate = jobs['backend']
    assert gate['name'] == 'Backend tests'
    assert set(gate['needs']) == {'changes', 'backend_suites', 'athena_browser'}
    assert 'always()' in gate['if']
    assert set(jobs['backend_suites']['strategy']['matrix']['suite']) == {'django', 'pytest'}
    command = gate['steps'][0]['run']
    process = subprocess.run(['bash', '-c', command], env={
        **os.environ,
        'CHANGES_RESULT': 'success',
        'BACKEND_RESULT': backend_result,
        'BROWSER_REQUIRED': browser_required,
        'BROWSER_RESULT': browser_result,
    })
    assert (process.returncode == 0) is passes


def test_required_backend_gate_runs_browser_when_detection_fails():
    import os
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    command = workflow['jobs']['backend']['steps'][0]['run']
    process = subprocess.run(['bash', '-c', command], env={
        **os.environ,
        'CHANGES_RESULT': 'failure',
        'BACKEND_RESULT': 'success',
        'BROWSER_REQUIRED': '',
        'BROWSER_RESULT': 'success',
    })
    assert process.returncode == 0


def test_backend_suites_are_bounded_and_do_not_migrate_twice():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    job = workflow['jobs']['backend_suites']
    assert job['timeout-minutes'] == 25
    steps = job['steps']
    assert all(step.get('name') != 'Run migrations' for step in steps)
    django_command = next(
        step['run'] for step in steps if step.get('name') == 'Run tests (Django runner)'
    )
    assert '--parallel auto' in django_command
    assert '--timing' in django_command
    assert all(step.get('name') != 'Verify deployed Athena browser runtime' for step in steps)
    setup_uv = next(step for step in steps if step.get('name') == 'Set up cached uv')
    assert setup_uv['uses'] == 'astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7'
    assert setup_uv['with']['version'] == '0.12.20'
    assert setup_uv['with']['enable-cache'] is True
    install = next(step['run'] for step in steps if step.get('name') == 'Install dependencies')
    assert install == 'uv pip install --system -r requirements.txt'
    pytest_command = next(
        step['run'] for step in steps if step.get('name') == 'Run tests (pytest)'
    )
    assert '--dist loadfile' in pytest_command
    assert '--durations=20' in pytest_command


def test_browser_runtime_is_a_separate_path_gated_job():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    job = workflow['jobs']['athena_browser']
    assert job['needs'] == 'changes'
    assert "outputs.browser_runtime == 'true'" in job['if']
    assert job['timeout-minutes'] == 20
    steps = job['steps']
    setup_uv = next(step for step in steps if step.get('name') == 'Set up cached uv')
    assert setup_uv['uses'] == 'astral-sh/setup-uv@c18668ad3cf93ea998bef934396af7bb5c839dc7'
    assert setup_uv['with']['version'] == '0.12.20'
    smoke = next(step for step in steps if step.get('name') == 'Install and launch Athena Chromium')
    assert smoke['run'] == 'python scripts/install_athena_browser.py --with-deps --require-package'
