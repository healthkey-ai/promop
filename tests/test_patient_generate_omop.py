"""Generate OMOP from one patient's Source Codes tab (#2822).

The run resolves that patient's rows against the current mappings and then
derives their PatientRecord, whether or not a row changed. The Patients list's
run keeps leaving the record to the resolver.
"""
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID
from omop_core.models import (
    Measurement, PatientRecord, PatientSourceCode, SourceCodeConceptMapping, SourceCodeResolveRun,
)
from omop_core.services import resolve_jobs
from omop_core.services.derivation_jobs import FakeDispatcher, InlineDispatcher, use_dispatcher
from omop_core.services.resolve_jobs import create_and_dispatch_resolve_run, run_bulk_resolve
from tests.factories import ConceptFactory, PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db

CODE = 'HGB-LOCAL'


@pytest.fixture()
def target(settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=32817, concept_code='32817', concept_name='EHR')
    return ConceptFactory(concept_name='Hemoglobin')


@pytest.fixture()
def fake():
    with use_dispatcher(FakeDispatcher()) as dispatcher:
        yield dispatcher


def _person(*, with_code=False):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    if with_code:
        PatientSourceCode.objects.create(
            person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='Local')
    return person


def _approved(target):
    return SourceCodeConceptMapping.objects.create(
        source_code=CODE, source_vocabulary_id='Local', source_code_description=CODE,
        domain_id='Measurement', omop_table='measurement', target_concept=target, status='approved')


def _staff_client():
    user = get_user_model().objects.create_user(email='generate-omop@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return client


class _FailingDispatcher(FakeDispatcher):
    def dispatch(self, person):
        super().dispatch(person)
        raise RuntimeError('lab value 13.2 g/dL is not a number')


# ---- the record is derived whether or not a row changed ---------------------------

def test_a_patients_run_derives_the_record_when_no_row_changed(target, fake):
    person = _person()

    run = create_and_dispatch_resolve_run([person.person_id], refresh_record=True)

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED
    assert (run.resolved, run.created, run.errors) == (0, 0, 0)
    assert fake.calls == [person.person_id]


def test_the_patients_lists_run_leaves_an_unchanged_patients_record_alone(target, fake):
    person = _person()

    create_and_dispatch_resolve_run([person.person_id])

    assert fake.calls == []


@pytest.mark.parametrize('refresh_record', [True, False])
def test_a_run_that_changes_rows_derives_the_record_once(target, fake, refresh_record):
    """The resolver's own derivation and the run's are not both made."""
    person = _person(with_code=True)
    _approved(target)

    run = create_and_dispatch_resolve_run([person.person_id], refresh_record=refresh_record)

    run.refresh_from_db()
    assert run.created == 1
    assert Measurement.objects.get(person=person).measurement_concept_id == target.concept_id
    assert fake.calls == [person.person_id]


def test_a_patients_run_rebuilds_a_record_left_stale(target):
    person = _person()
    PatientRecord.objects.filter(person=person).update(derivation_version=0)

    with use_dispatcher(InlineDispatcher()):
        run = create_and_dispatch_resolve_run([person.person_id], refresh_record=True)

    run.refresh_from_db()
    assert (run.state, run.errors) == (SourceCodeResolveRun.COMPLETED, 0)
    assert PatientRecord.objects.get(person=person).derivation_version != 0


def test_only_the_named_patient_is_resolved_and_derived(target, fake):
    person, other = _person(with_code=True), _person(with_code=True)
    _approved(target)

    create_and_dispatch_resolve_run([person.person_id], refresh_record=True)

    assert Measurement.objects.filter(person=person).count() == 1
    assert not Measurement.objects.filter(person=other).exists()
    assert fake.calls == [person.person_id]


# ---- failures -----------------------------------------------------------------------

def test_a_record_that_cannot_be_derived_is_reported_and_the_rows_stay(target):
    person = _person(with_code=True)
    _approved(target)

    with use_dispatcher(_FailingDispatcher()):
        run = create_and_dispatch_resolve_run([person.person_id], refresh_record=True)

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED
    assert (run.created, run.errors) == (1, 1)
    # The class of the error, never its text: that can quote a patient's value.
    assert run.error_detail == [
        {'person_id': person.person_id, 'step': 'record', 'error': 'RuntimeError'}]
    assert resolve_jobs.record_refresh_errors(run) == 1
    assert Measurement.objects.filter(person=person).count() == 1
    # Left stale, so a later derivation or backfill picks it up.
    assert PatientRecord.objects.get(person=person).derivation_version == 0

    body = _staff_client().get(f'/api/v1/resolve-runs/{run.id}/').json()
    assert (body['state'], body['errors'], body['record_refresh_errors']) == ('completed', 1, 1)
    assert '13.2' not in str(body)


def test_a_resolve_that_fails_still_has_its_committed_rows_derived(target, fake):
    person = _person()

    with mock.patch.object(resolve_jobs, 'resolve_person_source_codes', side_effect=ValueError('boom')):
        run = create_and_dispatch_resolve_run([person.person_id], refresh_record=True)

    run.refresh_from_db()
    assert (run.state, run.errors) == (SourceCodeResolveRun.COMPLETED, 1)
    assert resolve_jobs.record_refresh_errors(run) == 0
    assert fake.calls == [person.person_id]


def test_a_patient_who_no_longer_exists_is_an_error_and_nothing_is_derived(target, fake):
    run = create_and_dispatch_resolve_run([987654321], refresh_record=True)

    run.refresh_from_db()
    assert run.errors == 1
    assert resolve_jobs.record_refresh_errors(run) == 0
    assert fake.calls == []


# ---- on a worker --------------------------------------------------------------------

def test_on_a_worker_the_record_is_derived_in_the_run_not_queued_again(target, fake, settings):
    person = _person()
    run = SourceCodeResolveRun.objects.create(total=1)
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'

    with mock.patch('omop_core.services.patient_record_service.refresh_patient_record') as derive:
        run_bulk_resolve(run.id, [person.person_id], refresh_record=True)

    derive.assert_called_once_with(person)
    assert fake.calls == []
    run.refresh_from_db()
    assert (run.state, run.done, run.errors) == (SourceCodeResolveRun.COMPLETED, 1, 0)


@pytest.mark.parametrize('refresh_record, kwargs', [(True, {'refresh_record': True}), (False, {})])
def test_the_task_is_told_to_refresh_only_for_a_patients_run(
        target, settings, django_capture_on_commit_callbacks, refresh_record, kwargs):
    """A worker still on the previous release takes the list's runs unchanged."""
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'
    person = _person()

    with mock.patch('omop_core.tasks.bulk_resolve_source_codes_task.apply_async') as queued, \
            django_capture_on_commit_callbacks(execute=True):
        run = create_and_dispatch_resolve_run([person.person_id], refresh_record=refresh_record)

    queued.assert_called_once_with(args=[str(run.id), [person.person_id]], kwargs=kwargs)


def test_the_task_passes_the_refresh_on(target, fake):
    from omop_core.tasks import bulk_resolve_source_codes_task
    person = _person()
    run = SourceCodeResolveRun.objects.create(total=1)

    result = bulk_resolve_source_codes_task(str(run.id), [person.person_id], refresh_record=True)

    assert result['state'] == SourceCodeResolveRun.COMPLETED
    assert fake.calls == [person.person_id]


# ---- the endpoints ------------------------------------------------------------------

def test_the_patients_endpoint_generates_rows_and_then_derives(target, fake):
    person, other = _person(with_code=True), _person(with_code=True)
    _approved(target)
    client = _staff_client()

    response = client.post(f'/api/v1/patient-records/{person.person_id}/resolve-source-codes/', {},
                           format='json')

    assert response.status_code == 202
    body = client.get(f'/api/v1/resolve-runs/{response.json()["run_id"]}/').json()
    assert (body['state'], body['created'], body['errors'], body['record_refresh_errors']) == (
        'completed', 1, 0, 0)
    assert fake.calls == [person.person_id]
    assert not Measurement.objects.filter(person=other).exists()


def test_the_patients_endpoint_derives_when_nothing_changed(target, fake):
    person = _person()

    response = _staff_client().post(
        f'/api/v1/patient-records/{person.person_id}/resolve-source-codes/', {}, format='json')

    assert response.status_code == 202
    assert fake.calls == [person.person_id]


def test_the_patients_list_endpoint_still_derives_only_what_changed(target, fake):
    changed, unchanged = _person(with_code=True), _person()
    _approved(target)

    response = _staff_client().post(
        '/api/v1/patient-records/bulk-resolve-source-codes/',
        {'person_ids': [changed.person_id, unchanged.person_id]}, format='json')

    assert response.status_code == 202
    assert fake.calls == [changed.person_id]
