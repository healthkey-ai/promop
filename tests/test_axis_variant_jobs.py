"""The bulk axis variant refresh runs on the worker, behind a durable request (#2021)."""
import importlib
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.utils import timezone

from omop_core.models import AxisVariantRefresh, PatientSourceCode, SourceCodeConceptMapping
from omop_core.services import axis_variant_jobs
from tests.factories import PersonFactory

pytestmark = pytest.mark.django_db

LAB = 'ACME-LAB'
PUBLISH = 'omop_core.tasks.refresh_axis_variants_task.apply_async'


@pytest.fixture()
def two_unit_code():
    """A hospital glucose code seen in mg/dL and mmol/L: two LOINC properties."""
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU', omop_table='measurement')
    for unit in ('mg/dL', 'mmol/L'):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='GLU', source_vocabulary_id=LAB,
            omop_table='measurement', source_unit=unit, occurrence_count=3)
    return mapping


def test_a_request_is_recorded_and_published_once(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    with patch(PUBLISH) as publish:
        request, created = axis_variant_jobs.request_refresh('test')
        again, created_again = axis_variant_jobs.request_refresh('test')
    assert created and not created_again and again.pk == request.pk
    publish.assert_called_once_with(args=[request.pk], task_id=f'axis-variant-refresh-{request.pk}')
    request.refresh_from_db()
    assert (request.outcome, request.task_id, request.reason) == (
        'queued', f'axis-variant-refresh-{request.pk}', 'test')


def test_without_a_broker_the_request_waits_for_a_worker(settings):
    settings.CELERY_BROKER_URL = ''
    with patch(PUBLISH) as publish:
        request, _ = axis_variant_jobs.request_refresh('test')
    publish.assert_not_called()
    assert (request.outcome, request.task_id) == ('queued', '')


def test_an_unreachable_broker_leaves_the_request_queued(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    with patch(PUBLISH, side_effect=OSError('connection refused')):
        request, created = axis_variant_jobs.request_refresh('test')
    assert created
    request.refresh_from_db()
    assert (request.outcome, request.task_id) == ('queued', '')


def test_the_worker_claims_the_request_and_records_progress(two_unit_code):
    request = AxisVariantRefresh.objects.create(reason='test')
    result = axis_variant_jobs.run_refresh(request.pk, batch_size=1)
    request.refresh_from_db()
    two_unit_code.refresh_from_db()
    assert result == {'request_id': request.pk, 'skipped': False, 'outcome': 'done',
                      'keys': 1, 'refreshed': 1}
    assert (request.outcome, request.keys, request.done, request.refreshed) == ('done', 1, 1, 1)
    assert request.started_at is not None and request.completed_at is not None
    assert two_unit_code.axis_variant_count == 2


def test_a_request_that_is_not_queued_is_left_alone():
    request = AxisVariantRefresh.objects.create(reason='test', outcome='running')
    with patch.object(axis_variant_jobs.axis_variants, 'multi_unit_keys') as keys:
        result = axis_variant_jobs.run_refresh(request.pk)
    keys.assert_not_called()
    assert result == {'request_id': request.pk, 'skipped': True, 'outcome': 'running'}


def test_a_failure_is_recorded_and_raised():
    request = AxisVariantRefresh.objects.create(reason='test')
    with patch.object(axis_variant_jobs.axis_variants, 'multi_unit_keys', side_effect=RuntimeError('boom')):
        with pytest.raises(RuntimeError):
            axis_variant_jobs.run_refresh(request.pk)
    request.refresh_from_db()
    assert (request.outcome, request.failure_reason) == ('failed', 'boom')
    assert request.completed_at is not None


def test_a_ready_worker_publishes_queued_and_stranded_requests(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    dropped = AxisVariantRefresh.objects.create(reason='published to an old worker', task_id='axis-variant-refresh-1')
    stranded = AxisVariantRefresh.objects.create(
        reason='worker died', outcome='running', started_at=timezone.now() - timedelta(hours=7))
    live = AxisVariantRefresh.objects.create(reason='live', outcome='running', started_at=timezone.now())
    AxisVariantRefresh.objects.create(reason='done', outcome='done')
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes() == 2
    assert sorted(call.kwargs['args'][0] for call in publish.call_args_list) == sorted([dropped.pk, stranded.pk])
    stranded.refresh_from_db()
    live.refresh_from_db()
    assert (stranded.outcome, stranded.task_id) == ('queued', f'axis-variant-refresh-{stranded.pk}')
    assert live.outcome == 'running'


def test_the_task_performs_the_request(two_unit_code):
    from omop_core.tasks import refresh_axis_variants_task

    request = AxisVariantRefresh.objects.create(reason='test')
    assert refresh_axis_variants_task(request.pk)['outcome'] == 'done'
    two_unit_code.refresh_from_db()
    assert two_unit_code.axis_variant_count == 2


def test_migration_0303_requests_one_refresh(settings, two_unit_code):
    settings.CELERY_BROKER_URL = ''
    migration = importlib.import_module('omop_core.migrations.0303_axis_variant_refresh')
    migration.request_refresh(apps, None)
    migration.request_refresh(apps, None)
    request = AxisVariantRefresh.objects.get()
    assert (request.outcome, request.reason) == ('queued', migration.REASON)
    two_unit_code.refresh_from_db()
    assert two_unit_code.axis_variant_count == 0, 'migrate must not perform the refresh'
    migration.withdraw_refresh(apps, None)
    assert not AxisVariantRefresh.objects.exists()


def test_the_command_can_queue_instead_of_refreshing(settings, two_unit_code):
    settings.CELERY_BROKER_URL = ''
    out = StringIO()
    call_command('refresh_axis_variants', '--queue', stdout=out)
    call_command('refresh_axis_variants', '--queue', stdout=out)
    request = AxisVariantRefresh.objects.get()
    assert f'Queued axis variant refresh {request.pk}' in out.getvalue()
    assert f'refresh {request.pk} is already queued' in out.getvalue()
    two_unit_code.refresh_from_db()
    assert two_unit_code.axis_variant_count == 0
