"""A mapping save queues the stored-row re-point; a job moves the rows."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.models import SourceCodeConceptMapping
from omop_core.services import repoint_jobs
from tests.factories import ConceptFactory, MeasurementFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def client(django_user_model):
    instance = APIClient()
    instance.force_authenticate(django_user_model.objects.create_user(email='repoint-jobs@test.com', is_staff=True))
    return instance


def mapping(target, **overrides):
    return SourceCodeConceptMapping.objects.create(**{
        'source_code': 'GLU', 'source_vocabulary_id': 'Local', 'source_code_description': 'Glucose',
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'target_concept': target,
        'status': 'approved', **overrides,
    })


def test_the_save_returns_with_rows_unmoved_and_the_commit_moves_them(client, django_capture_on_commit_callbacks):
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    stored = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    with patch.object(repoint_jobs, 'repoint_clinical_rows', wraps=repoint_jobs.repoint_clinical_rows) as moved, \
            django_capture_on_commit_callbacks() as callbacks:
        response = client.patch(f'/api/v1/code-mappings/{row.pk}/',
                                {'status': 'approved', 'destination_concept_id': new.pk}, format='json')
        assert response.status_code == 200, response.data
        moved.assert_not_called()
    assert response.data['repoint'] == {'queued': True, 'from_concept_ids': [old.pk]}
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk

    for callback in callbacks:
        callback()
    stored.refresh_from_db(); row.refresh_from_db()
    assert stored.measurement_concept_id == new.pk
    assert row.deferred_repoint_concept_ids == []


def test_a_broker_queues_the_task_and_a_queue_outage_runs_it_inline(settings):
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[old.pk])
    stored = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    with patch('omop_core.tasks.repoint_mapping_rows_task.delay') as delay:
        repoint_jobs.dispatch_repoint(row.pk)
    delay.assert_called_once_with(row.pk)
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk

    with patch('omop_core.tasks.repoint_mapping_rows_task.delay', side_effect=ConnectionError('no broker')):
        repoint_jobs.dispatch_repoint(row.pk)
    stored.refresh_from_db()
    assert stored.measurement_concept_id == new.pk


def test_a_re_point_during_the_run_carries_the_rows_on_to_the_newest_destination():
    first, second, third = ConceptFactory(), ConceptFactory(), ConceptFactory()
    row = mapping(second, deferred_repoint_concept_ids=[first.pk])
    stored = MeasurementFactory(measurement_concept=first, measurement_source_value='GLU')
    real = repoint_jobs.repoint_clinical_rows
    calls = []

    def save_lands_mid_run(**kwargs):
        outcome = real(**kwargs)
        if not calls:
            # What a second save does while the first pass is moving rows.
            SourceCodeConceptMapping.objects.filter(pk=row.pk).update(
                target_concept=third, deferred_repoint_concept_ids=[first.pk, second.pk])
        calls.append(kwargs['new_concept_id'])
        return outcome

    with patch.object(repoint_jobs, 'repoint_clinical_rows', side_effect=save_lands_mid_run):
        repoint_jobs.run_repoint(row.pk)

    stored.refresh_from_db(); row.refresh_from_db()
    assert stored.measurement_concept_id == third.pk
    assert row.deferred_repoint_concept_ids == []
    assert calls[0] == second.pk and set(calls[1:]) == {third.pk}


def test_an_unapproved_mapping_keeps_its_rows_and_its_queue():
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(new, status='proposed', deferred_repoint_concept_ids=[old.pk])
    stored = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 0

    stored.refresh_from_db(); row.refresh_from_db()
    assert stored.measurement_concept_id == old.pk
    assert row.deferred_repoint_concept_ids == [old.pk]


def test_a_duplicate_run_finds_nothing_left_to_move():
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[old.pk])
    MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 1
    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 0
