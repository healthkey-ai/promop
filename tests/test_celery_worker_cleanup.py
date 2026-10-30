"""A stalled warm shutdown must not leave a worker/database session behind."""
import signal
import subprocess
import sys

import pytest

from tests.test_celery_e2e import _stop_worker


@pytest.mark.parametrize('ignore_term', [False, True])
def test_stop_worker_reaps_graceful_and_unresponsive_processes(ignore_term):
    # Readiness makes the timeout path deterministic: SIGTERM is ignored before
    # the parent attempts cleanup, rather than racing child initialization.
    program = (
        'import signal, time\n'
        + ('signal.signal(signal.SIGTERM, signal.SIG_IGN)\n' if ignore_term else '')
        + 'print("ready", flush=True)\ntime.sleep(60)\n'
    )
    worker = subprocess.Popen([sys.executable, '-c', program], stdout=subprocess.PIPE, text=True)
    try:
        assert worker.stdout.readline().strip() == 'ready'
        _stop_worker(worker, timeout=0.2)
        assert worker.poll() == -(signal.SIGKILL if ignore_term else signal.SIGTERM)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=10)
        worker.stdout.close()
