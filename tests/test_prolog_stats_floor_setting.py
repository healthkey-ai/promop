"""promop passes PROLOG_STATS_MIN_COUNT through to PROlog (0.8.0, RUN-24; #2788)."""
import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.django_db


def _check(value: str) -> subprocess.CompletedProcess:
    env = {**os.environ, 'PROLOG_STATS_MIN_COUNT': value}
    return subprocess.run([sys.executable, 'manage.py', 'check'], env=env, capture_output=True, text=True, timeout=300)


def test_the_floor_is_read_from_the_environment(settings):
    from prolog_surveys import conf

    settings.PROLOG_STATS_MIN_COUNT = 20
    assert conf.get('PROLOG_STATS_MIN_COUNT') == 20


@pytest.mark.parametrize('value,ok', [('20', True), ('-1', False), ('x', False)])
def test_a_bad_floor_stops_startup(value, ok):
    result = _check(value)
    assert (result.returncode == 0) is ok, result.stderr[-500:]
