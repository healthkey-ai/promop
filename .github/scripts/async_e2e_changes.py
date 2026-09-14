"""Select CI suites from PR changes or the full deployment push diff."""

import ast
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess


# These Markdown files are consumed by import/build commands as runtime data.
# Keep them under full CI even though their extension and directory look like docs.
RUNTIME_DOCUMENTS = {
    "docs/code-concept-mappings.md",
    "docs/ht-code-concept-mapping.md",
    "docs/ht-fhir-code-concept-mapping.md",
}
DOC_EXTENSIONS = {".md", ".rst", ".adoc"}
DOC_ASSET_EXTENSIONS = {".txt", ".pdf", ".png", ".jpg", ".jpeg", ".svg", ".webp"}


def is_docs_only(paths):
    """An empty or unrecognized change set must retain normal CI."""
    if not paths:
        return False
    for name in paths:
        path = PurePosixPath(name)
        if name in RUNTIME_DOCUMENTS:
            return False
        if name in {"LICENSE", "NOTICE"}:
            continue
        if path.suffix in DOC_EXTENSIONS and (
            path.parent == PurePosixPath(".") or name.startswith(("docs/", ".github/"))
        ):
            continue
        if name.startswith("docs/") and path.suffix in DOC_ASSET_EXTENSIONS:
            continue
        return False
    return True


FRONTEND_EXTENSIONS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".css", ".scss",
    ".html", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".map", ".md",
}
FRONTEND_CONFIG_NAMES = {".gitignore", ".npmrc", ".nvmrc", ".browserslistrc"}


def requires_backend(paths):
    """Skip only recognized frontend/doc paths; unknown or mixed changes run."""
    if not paths:
        return True
    for name in paths:
        path = PurePosixPath(name)
        if is_docs_only([name]):
            continue
        if name.startswith("frontend/") and (
            path.suffix in FRONTEND_EXTENSIONS or path.name in FRONTEND_CONFIG_NAMES
        ):
            continue
        return True
    return False


# Deliberately scoped to async execution, not all code a task might call.
# Synchronous derivation/ranking/model changes remain covered by backend tests.
ASYNC_PATHS = (
    "**/tasks.py", "**/tasks/*.py", "**/celery.py",
    "promop/__init__.py", "ctomop/**", "start-worker.sh",
    "omop_core/services/derivation_jobs.py",
    "omop_core/services/suggest_jobs.py",
    "omop_core/services/embedding_jobs.py",
    "tests/test_celery_e2e.py", "tests/test_async_e2e_changes.py",
    ".github/scripts/async_e2e_changes.py", ".github/workflows/ci.yml",
)

# These files mix async and unrelated code. Compare only the async definitions
# so editing, for example, a profile endpoint does not start a Redis worker.
ASYNC_DEFINITIONS = {
    "patient_portal/api/views.py": {
        "PatientRecordV1ViewSet.refresh", "derivation_status",
        "code_mapping_suggest", "_serialize_suggest_run",
        "code_mapping_latest_suggest_run", "code_mapping_suggest_run",
    },
    "omop_core/signals.py": {"field_concept_mapping_saved", "_dispatch_projection"},
    "omop_core/models.py": {"SuggestRun"},
}
ASYNC_WORDS = (
    "celery", "redis", "kombu", "amqp", "billiard", "start-worker",
    "derivation_jobs", "suggest_jobs", "embedding_jobs", "get_dispatcher",
    "get_suggest_dispatcher", "derivation_status", "derivation-status",
    "suggest_run", "suggest-run", "code_mapping_suggest", "shared_task",
    ".delay(", ".apply_async(", ".send_task(", "_dispatch_projection",
)


def mentions_async(text):
    return any(word in text.lower() for word in ASYNC_WORDS)


def async_signature(path, source):
    # Dependencies, routing, environment and deployment files can mention
    # worker/broker settings anywhere. Preserve matching lines in their order.
    source_lines = source.splitlines()
    selected = set()
    for index, line in enumerate(source_lines):
        if line.lstrip().startswith("#"):
            continue
        worker_service = re.match(r"\s*- type: (worker|redis|keyvalue)\s*$", line)
        if mentions_async(line) or worker_service:
            selected.add(index)
            # Include multiline values, e.g. a Render CELERY_* key followed
            # by value/fromService, and entire worker/Redis service blocks.
            if path.endswith((".yaml", ".yml")):
                indent = len(line) - len(line.lstrip())
                for following in range(index + 1, len(source_lines)):
                    child = source_lines[following]
                    if child.strip() and len(child) - len(child.lstrip()) <= indent:
                        break
                    selected.add(following)
    lines = tuple(source_lines[index].strip() for index in sorted(selected))
    definitions = []
    if path in ASYNC_DEFINITIONS:
        def visit(nodes, prefix=""):
            for node in nodes:
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = prefix + node.name
                    if name in ASYNC_DEFINITIONS[path]:
                        definitions.append((name, ast.dump(node)))
                    elif isinstance(node, ast.ClassDef):
                        visit(node.body, name + ".")
        visit(ast.parse(source).body)
    elif path == "promop/settings.py":
        # Include whole multi-line settings expressions, not just CELERY_*'s
        # first line. Worker-specific boot guards are also covered.
        for node in ast.parse(source).body:
            if mentions_async(ast.get_source_segment(source, node) or ""):
                definitions.append(ast.dump(node))
    return lines, definitions


def requires_async_e2e(paths, base=None, head=None):
    for path in paths:
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in ASYNC_PATHS):
            return True
        # UI/docs never configure the backend worker. Other text files are
        # checked for async-specific changes, including newly added dispatchers.
        if path.startswith(("frontend/", "docs/")) or path.endswith(".md"):
            continue
        if path == "docker-compose.bridge.yml":
            continue
        if base is not None and head is not None:
            if async_signature(path, file_at(base, path)) != async_signature(path, file_at(head, path)):
                return True
    return False


def file_at(revision, path):
    # A deleted/renamed file is absent on one side of the comparison.
    exists = subprocess.run(["git", "cat-file", "-e", f"{revision}:{path}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exists.returncode:
        return ""
    return subprocess.check_output(["git", "show", f"{revision}:{path}"]).decode(
        "utf-8", errors="replace")


def select_checks(base, head, *, merge_base=True):
    if merge_base:
        base = subprocess.check_output(["git", "merge-base", base, head], text=True).strip()
    paths = changed_paths(base, head, merge_base=False)
    docs_only = is_docs_only(paths)
    return (False if docs_only else requires_async_e2e(paths, base, head)), docs_only, requires_backend(paths)


def select_range(base, head, *, merge_base=True):
    return select_checks(base, head, merge_base=merge_base)[0]


def changed_paths(base, head, *, merge_base=True):
    # Three-dot diff excludes changes made only on the base branch. Disable
    # rename detection so moving a backend file into an ignored path still
    # includes its deleted backend path. NUL delimiters preserve any filename.
    comparison = f"{base}...{head}" if merge_base else f"{base}..{head}"
    result = subprocess.run(
        ["git", "diff", "--no-renames", "--name-only", "-z",
         comparison, "--"],
        check=True, capture_output=True,
    )
    return [os.fsdecode(name) for name in result.stdout.split(b"\0") if name]


def main():
    run, docs_only, backend = True, False, True
    event_name = os.environ["GITHUB_EVENT_NAME"]
    if event_name in {"pull_request", "push"}:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    if event_name == "pull_request":
        pr = event["pull_request"]
        run, docs_only, backend = select_checks(pr["base"]["sha"], pr["head"]["sha"])
    elif event_name == "push":
        # Reusable workflows keep the caller's push event. Compare both ends
        # of the entire push, not HEAD^ (which would miss multi-commit pushes).
        before, after = event.get("before"), event.get("after")
        if before and after and before != "0" * 40 and after != "0" * 40:
            run, docs_only, backend = select_checks(before, after, merge_base=False)
    output = f"async_e2e={str(run).lower()}\ndocs_only={str(docs_only).lower()}\nbackend={str(backend).lower()}"
    print(output)
    with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
        stream.write(output + "\n")


if __name__ == "__main__":
    main()
