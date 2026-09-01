"""The production settings guard must not stop a Celery worker from booting.

The guard demands ALLOWED_HOSTS and CORS_ALLOWED_ORIGINS, which a worker has
no use for. Without the exemption the worker crashes on import and the queue
is never consumed, which looks like a hung refresh rather than a config error.
"""

import os
import subprocess
import sys

_PROD_ENV = {
    'DEBUG': 'False',
    'SECRET_KEY': 'a-real-looking-secret-for-this-test',
    'DATABASE_URL': 'postgresql://postgres@localhost:5432/promop_test',
    'ALLOWED_HOSTS': '',
    'CORS_ALLOWED_ORIGINS': '',
}

# argv[0] is what settings.py inspects, so it is spoofed rather than actually
# running the celery CLI, which would then try to reach a broker.
_BOOT = (
    'import sys; sys.argv = [{argv0!r}]; '
    'import django; django.setup()'
)


def _boot(argv0: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **_PROD_ENV, 'DJANGO_SETTINGS_MODULE': 'ctomop.settings'}
    return subprocess.run(
        [sys.executable, '-c', _BOOT.format(argv0=argv0)],
        env=env, capture_output=True, text=True,
    )


def test_a_worker_boots_without_the_http_settings():
    result = _boot('/usr/local/bin/celery')

    assert result.returncode == 0, result.stderr


def test_the_web_process_still_has_to_declare_them():
    result = _boot('/usr/local/bin/gunicorn')

    assert result.returncode != 0
    assert 'ALLOWED_HOSTS' in result.stderr
    assert 'CORS_ALLOWED_ORIGINS' in result.stderr
