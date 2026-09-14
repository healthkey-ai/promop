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
    "omop_core/tasks.py", "another_app/tasks/email.py", "ctomop/celery.py",
    "ctomop/__init__.py", "omop_core/services/derivation_jobs.py",
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
    ("push", ["ctomop/celery.py"], "true"),
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
        return filter_module.requires_async_e2e(paths), filter_module.is_docs_only(paths), filter_module.requires_backend(paths)

    monkeypatch.setattr(filter_module, "select_checks", select_checks)
    filter_module.main()
    docs_only = event_name in {"pull_request", "push"} and filter_module.is_docs_only(paths)
    backend = event_name not in {"pull_request", "push"} or filter_module.requires_backend(paths)
    assert output.read_text() == f"async_e2e={expected}\ndocs_only={str(docs_only).lower()}\nbackend={str(backend).lower()}\n"


def test_new_branch_push_runs_without_a_comparison(tmp_path, monkeypatch):
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": "0" * 40, "after": "new-head"}))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    filter_module.main()
    assert output.read_text() == "async_e2e=true\ndocs_only=false\nbackend=true\n"


@pytest.mark.parametrize("path,before,after,expected", [
    ("patient_portal/api/views.py", "def profile(): return 1", "def profile(): return 2", False),
    ("patient_portal/api/views.py", "def derivation_status(): return 1", "def derivation_status(): return 2", True),
    ("patient_portal/api/views.py", "class PatientRecordV1ViewSet:\n def refresh(self): return 1",
     "class PatientRecordV1ViewSet:\n def refresh(self): return 2", True),
    ("patient_portal/api/views.py", "def code_mapping_suggest_run(): return 1",
     "def code_mapping_suggest_run(): return 2", True),
    ("ctomop/settings.py", "DEBUG = True", "DEBUG = False", False),
    ("ctomop/settings.py", "CELERY_BROKER_TRANSPORT_OPTIONS = {\n 'visibility_timeout': 100\n}",
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
    ["field_concept_mapping_plan.md", "field_concept_mapping_architecture.md"],
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
    assert filter_module.select_checks(base, "HEAD") == (False, True, False)
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


@pytest.mark.parametrize('result,passes', [
    ('success', True), ('failure', False), ('cancelled', False),
    ('skipped', False), ('', False),
])
def test_required_backend_gate_rejects_incomplete_matrix(result, passes):
    import os
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    jobs = workflow['jobs']
    gate = jobs['backend']
    assert gate['name'] == 'Backend tests'
    assert set(gate['needs']) == {'changes', 'backend_suites'}
    assert 'always()' in gate['if']
    assert set(jobs['backend_suites']['strategy']['matrix']['suite']) == {'django', 'pytest'}
    command = gate['steps'][0]['run']
    process = subprocess.run(['bash', '-c', command], env={**os.environ, 'BACKEND_RESULT': result})
    assert (process.returncode == 0) is passes
