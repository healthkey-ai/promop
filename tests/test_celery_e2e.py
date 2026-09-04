"""End to end: refresh/ queues on Redis, a real worker derives, polling sees it.

Needs a broker, so it is marked e2e and deselected by default. CI runs it in
its own job — the two backend suites already share the test database name and
cannot run at the same time.
"""

import os
import subprocess
import time

import pytest
from django.db import connection

from omop_core.models import PatientRecord, Person
from patient_portal.models import Identity

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

_POLL_TIMEOUT_SECONDS = 90


def _test_database_url() -> str:
    """The database pytest built, spelled so a subprocess can reach it.

    The worker is a separate process and reads DATABASE_URL, so it would
    otherwise connect to the development database and never see the fixtures.
    """
    cfg = connection.settings_dict
    user = cfg['USER'] or 'postgres'
    password = f":{cfg['PASSWORD']}" if cfg['PASSWORD'] else ''
    host = cfg['HOST'] or 'localhost'
    port = cfg['PORT'] or '5432'
    return f"postgresql://{user}{password}@{host}:{port}/{cfg['NAME']}"


@pytest.fixture
def celery_worker_process():
    broker = os.environ.get('CELERY_BROKER_URL', '')
    if not broker:
        pytest.skip('CELERY_BROKER_URL is not set')

    env = {**os.environ, 'DATABASE_URL': _test_database_url()}
    # solo pool: one process, so a failure surfaces in this worker's own output
    # instead of in a forked child nobody is reading.
    worker = subprocess.Popen(
        ['celery', '-A', 'ctomop', 'worker', '--loglevel=info', '--pool=solo',
         '--without-gossip', '--without-mingle', '--without-heartbeat'],
        env=env,
    )
    try:
        yield worker
    finally:
        worker.terminate()
        worker.wait(timeout=30)


def _poll(client, task_id: str) -> dict:
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    last = None
    while time.monotonic() < deadline:
        resp = client.get(f'/api/v1/derivation-status/{task_id}/')
        assert resp.status_code == 200, resp.data
        last = resp.data
        if last['state'] in ('SUCCESS', 'FAILURE'):
            return last
        time.sleep(0.5)
    raise AssertionError(f'derivation never finished, last state: {last}')


def test_refresh_is_derived_by_a_real_worker(celery_worker_process):
    from rest_framework.test import APIClient

    person = Person.objects.create(person_id=770001, year_of_birth=1970)
    record = PatientRecord.objects.create(person=person)
    assert record.derived_at is None

    staff = Identity.objects.create_user(
        email='celery-e2e@example.test', password='pw', is_staff=True)
    client = APIClient()
    client.force_authenticate(user=staff)

    resp = client.post(f'/api/v1/patient-records/{person.person_id}/refresh/')
    assert resp.status_code == 202, resp.data

    state = _poll(client, resp.data['task_id'])

    assert state['state'] == 'SUCCESS', state
    record.refresh_from_db()
    assert record.derived_at is not None
