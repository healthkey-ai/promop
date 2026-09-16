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
