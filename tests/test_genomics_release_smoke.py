from urllib.parse import urlsplit

import pytest
from rest_framework.test import APIClient

from omop_core.models import Measurement, Observation, PatientRecord
from scripts.genomics_release_smoke import run_smoke
from tests.test_genomics_crud import setup  # noqa: F401

pytestmark = pytest.mark.django_db


class Session:
    def __init__(self, staff, fail_patch=False):
        self.client = APIClient()
        self.client.force_authenticate(staff)
        self.fail_patch = fail_patch

    def request(self, method, url, json=None, timeout=None):
        if method == 'PATCH' and self.fail_patch:
            self.fail_patch = False
            raise RuntimeError('simulated interruption')
        u = urlsplit(url)
        path = u.path + ('?' + u.query if u.query else '')
        return getattr(self.client, method.lower())(path, json, format='json')


def prepare(record):
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={'genomics_release_smoke': 'test-run'})


def test_smoke_exercises_real_api_and_retains_retired_history(setup):
    person, record, staff = setup
    prepare(record)
    report = run_smoke(Session(staff), 'http://testserver', person.pk, 'test-run')
    assert report['passed'] and report['cleanup_passed']
    assert Measurement.objects.filter(person=person, is_erroneous=True).exists()
    assert Observation.objects.filter(person=person, is_erroneous=True).exists()
    assert not Measurement.objects.filter(person=person, is_erroneous=False).exists()


def test_smoke_cleans_up_after_interrupted_edit(setup):
    person, record, staff = setup
    prepare(record)
    with pytest.raises(RuntimeError, match='simulated interruption'):
        run_smoke(Session(staff, fail_patch=True), 'http://testserver', person.pk, 'test-run')
    assert not Measurement.objects.filter(person=person, is_erroneous=False).exists()


def test_smoke_refuses_unmarked_record(setup):
    person, _, staff = setup
    with pytest.raises(RuntimeError, match='not provisioned'):
        run_smoke(Session(staff), 'http://testserver', person.pk, 'wrong-run')
    assert not Measurement.objects.filter(person=person).exists()
