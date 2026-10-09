"""End to end: refresh/ queues on Redis, a real worker derives, polling sees it.

Needs a broker, so it is marked e2e and deselected by default. CI runs it in
its own job — the two backend suites already share the test database name and
cannot run at the same time.
"""

import os
import subprocess
import sys
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


@pytest.fixture(params=['promop', 'ctomop'])
def celery_worker_process(request):
    broker = os.environ.get('CELERY_BROKER_URL', '')
    if not broker:
        pytest.skip('CELERY_BROKER_URL is not set')

    env = {**os.environ, 'DATABASE_URL': _test_database_url()}
    # solo pool: one process, so a failure surfaces in this worker's own output
    # instead of in a forked child nobody is reading.
    worker = subprocess.Popen(
        [sys.executable, '-m', 'celery', '-A', request.param, 'worker', '--loglevel=info', '--pool=solo',
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


def test_suggest_queues_one_hundred_codes_for_a_real_worker(celery_worker_process):
    """A broker-backed API run processes all 100 rows instead of the inline 3."""
    from rest_framework.test import APIClient
    from omop_core.models import SourceCodeConceptMapping

    staff = Identity.objects.create_user(
        email='suggest-e2e@example.test', password='pw', is_staff=True)
    client = APIClient()
    client.force_authenticate(user=staff)
    # No UMLS fixtures: each row deterministically finishes without a candidate,
    # requiring neither an external ranking API nor a downloaded vector model.
    SourceCodeConceptMapping.objects.bulk_create([
        SourceCodeConceptMapping(
            source_vocabulary_id='ICD10CM', source_code=f'E2E.{i:03d}',
            domain_id='Condition', omop_table='condition', status='proposed',
            occurrence_count=100-i,
        ) for i in range(100)
    ])
    reference = client.get('/api/v1/code-mappings/reference/')
    assert reference.status_code == 200
    assert reference.data['suggest_max_per_run'] == 100
    response = client.post('/api/v1/code-mappings/suggest/', {
        'source_vocabulary_id': 'ICD10CM', 'limit': 100, 'strategies': ['umls'],
    }, format='json')
    assert response.status_code == 202, response.data
    assert response.data['total'] == 100
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(
            f'/api/v1/code-mappings/suggest-runs/{response.data["run_id"]}/')
        assert response.status_code == 200, response.data
        if response.data['state'] in ('success', 'failure'):
            break
        time.sleep(0.5)
    assert response.data['state'] == 'success', response.data
    assert response.data['done'] == 100
    assert SourceCodeConceptMapping.objects.filter(
        source_code__startswith='E2E.', last_suggest_attempt__isnull=False,
    ).count() == 100
