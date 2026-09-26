"""Exercise the Render entrypoint without changing a database or starting a server."""

import os
from pathlib import Path
import subprocess

from django.core.management import get_commands
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failed_command", [None, "check", "prepare_production_database", "setup_admin"])
def test_web_startup_commands_and_failure_gates(tmp_path, failed_command):
    log = tmp_path / "commands"
    for executable, body in {
        "python": 'echo "$*" >> "$STARTUP_LOG"\n'
        'if [ "$2" = "$FAIL_COMMAND" ]; then exit 19; fi\n',
        "gunicorn": 'echo "gunicorn $*" >> "$STARTUP_LOG"\n',
    }.items():
        shim = tmp_path / executable
        shim.write_text("#!/bin/sh\n" + body)
        shim.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "start.sh")],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "STARTUP_LOG": str(log),
            "FAIL_COMMAND": failed_command or "",
            "ATHENA_VOCABULARY_GDRIVE_URL": "https://example.test/athena",
        },
        capture_output=True,
        text=True,
    )
    commands = log.read_text().splitlines()
    # Catch retired/misspelled management commands even though execution is stubbed.
    for command in commands:
        if command.startswith("manage.py "):
            assert command.split()[1] in get_commands(), command
    expected = [
        "manage.py check --deploy --fail-level ERROR",
        "manage.py prepare_production_database --gdrive https://example.test/athena",
        "manage.py setup_admin",
        "gunicorn promop.wsgi:application",
    ]
    if failed_command:
        last = next(i for i, command in enumerate(expected) if command.split()[1] == failed_command)
        assert commands == expected[:last + 1]
        assert result.returncode == 19
    else:
        assert commands == expected
        assert result.returncode == 0


def test_start_sh_does_not_choose_the_settings_module(tmp_path):
    """The entrypoint must leave the settings module to manage.py and wsgi.py.

    The security-review gate leaves start.sh ungated and gates promop/wsgi.py,
    promop/celery.py and manage.py, on the grounds that the
    `os.environ.setdefault('DJANGO_SETTINGS_MODULE', ...)` in those three is
    what selects production's settings. A single `export DJANGO_SETTINGS_MODULE=`
    line here would override all three from outside the gate, so that premise
    has to be pinned rather than assumed. The argv form is already pinned by the
    exact-sequence assertion above: a `--settings=` appended to any manage.py
    line fails it. This covers the environment form, which nothing else reaches.
    """
    log = tmp_path / "commands"
    for executable in ("python", "gunicorn"):
        shim = tmp_path / executable
        shim.write_text(
            '#!/bin/sh\necho "${DJANGO_SETTINGS_MODULE-<unset>}" >> "$STARTUP_LOG"\n'
        )
        shim.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    subprocess.run(
        ["bash", str(ROOT / "start.sh")],
        cwd=ROOT,
        env={
            **env,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "STARTUP_LOG": str(log),
            "FAIL_COMMAND": "",
            "ATHENA_VOCABULARY_GDRIVE_URL": "https://example.test/athena",
        },
        capture_output=True,
        text=True,
    )
    observed = set(log.read_text().split())
    assert observed == {"<unset>"}, observed
