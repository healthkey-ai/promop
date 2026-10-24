"""The bulk axis variant refresh runs on the worker, behind a durable request (#2021)."""
import importlib
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.apps import apps
from django.core.management import call_command
from django.db import transaction
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
    assert request.task_id == f'axis-variant-refresh-{request.pk}', 'the instance learns its task id too'
    request.refresh_from_db()
    assert (request.outcome, request.task_id, request.reason) == (
        'queued', f'axis-variant-refresh-{request.pk}', 'test')


def test_two_requests_cannot_be_active_at_once():
    from django.db import IntegrityError

    AxisVariantRefresh.objects.create(reason='first')
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            AxisVariantRefresh.objects.create(reason='second')
    AxisVariantRefresh.objects.create(reason='done', outcome='done')
    AxisVariantRefresh.objects.create(reason='also done', outcome='done')


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
    assert request.progress_at is not None and request.progress_at >= request.started_at
    assert two_unit_code.axis_variant_count == 2


def test_a_requeued_request_resumes_where_it_stopped(two_unit_code):
    """GLU sorts before GLU2; a request killed after GLU resumes with GLU2 only."""
    other = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU2', omop_table='measurement')
    for unit in ('mg/dL', 'mmol/L'):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='GLU2', source_vocabulary_id=LAB,
            omop_table='measurement', source_unit=unit, occurrence_count=3)
    request = AxisVariantRefresh.objects.create(reason='requeued', done=1, refreshed=1)
    result = axis_variant_jobs.run_refresh(request.pk, batch_size=1)
    request.refresh_from_db()
    two_unit_code.refresh_from_db()
    other.refresh_from_db()
    assert result['outcome'] == 'done' and result['keys'] == 2 and result['refreshed'] == 2
    assert (request.done, request.refreshed) == (2, 2)
    assert (two_unit_code.axis_variant_count, other.axis_variant_count) == (0, 2), \
        'the code before the offset was not walked again'


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


def test_a_ready_worker_republishes_a_dropped_request_and_leaves_a_live_one(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    dropped = AxisVariantRefresh.objects.create(reason='published to an old worker', task_id='axis-variant-refresh-1')
    live = AxisVariantRefresh.objects.create(
        reason='live', outcome='running', started_at=timezone.now() - timedelta(hours=3),
        progress_at=timezone.now() - timedelta(minutes=2))
    AxisVariantRefresh.objects.create(reason='done', outcome='done')
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes() == 1
    publish.assert_called_once_with(args=[dropped.pk], task_id=f'axis-variant-refresh-{dropped.pk}')
    live.refresh_from_db()
    assert live.outcome == 'running', 'a walk that reported progress two minutes ago is alive'


def test_a_ready_worker_requeues_a_request_whose_heartbeat_stopped(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    stranded = AxisVariantRefresh.objects.create(
        reason='worker replaced by a deploy', outcome='running', keys=40000, done=12500, refreshed=900,
        started_at=timezone.now() - timedelta(hours=1), progress_at=timezone.now() - timedelta(minutes=20))
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes() == 1
    publish.assert_called_once()
    stranded.refresh_from_db()
    assert (stranded.outcome, stranded.task_id) == ('queued', f'axis-variant-refresh-{stranded.pk}')
    assert (stranded.done, stranded.refreshed) == (12500, 900), 'the offset survives the requeue'


def test_a_running_request_from_before_the_heartbeat_counts_as_stranded(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    old = AxisVariantRefresh.objects.create(
        reason='claimed by the previous release', outcome='running',
        started_at=timezone.now() - timedelta(minutes=20), progress_at=None)
    with patch(PUBLISH):
        assert axis_variant_jobs.dispatch_queued_refreshes() == 1
    old.refresh_from_db()
    assert old.outcome == 'queued'


def test_a_stranded_request_is_closed_when_another_is_already_waiting(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    stranded = AxisVariantRefresh.objects.create(
        reason='worker died', outcome='running', started_at=timezone.now() - timedelta(hours=7),
        progress_at=timezone.now() - timedelta(hours=7))
    waiting = AxisVariantRefresh.objects.create(reason='waiting')
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes() == 1
    publish.assert_called_once_with(args=[waiting.pk], task_id=f'axis-variant-refresh-{waiting.pk}')
    stranded.refresh_from_db()
    assert stranded.outcome == 'failed' and str(waiting.pk) in stranded.failure_reason


def test_a_recheck_does_not_republish_what_was_published(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    AxisVariantRefresh.objects.create(reason='published', task_id='axis-variant-refresh-1')
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes(republish=False) == 0
    publish.assert_not_called()
    AxisVariantRefresh.objects.filter(reason='published').update(task_id='')
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes(republish=False) == 1


def test_the_task_limits_agree_with_the_stranded_cutoff():
    from omop_core.tasks import refresh_axis_variants_task

    assert refresh_axis_variants_task.time_limit == axis_variant_jobs.TIME_LIMIT
    assert refresh_axis_variants_task.soft_time_limit < axis_variant_jobs.TIME_LIMIT


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


def test_migration_0303_publishes_only_after_commit(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    migration = importlib.import_module('omop_core.migrations.0303_axis_variant_refresh')
    with patch(PUBLISH) as publish:
        with transaction.atomic():
            migration.request_refresh(apps, None)
            publish.assert_not_called()
    # The test transaction never commits, so the publish never fires here; the
    # row is what a worker's sweep would pick up.
    assert AxisVariantRefresh.objects.get().outcome == 'queued'


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


def test_the_inline_command_completes_a_queued_request(two_unit_code):
    queued = AxisVariantRefresh.objects.create(reason='left by a migration')
    out = StringIO()
    call_command('refresh_axis_variants', stdout=out)
    queued.refresh_from_db()
    two_unit_code.refresh_from_db()
    assert queued.outcome == 'done' and two_unit_code.axis_variant_count == 2
    assert 'Refreshed 1 mappings; 1 now have axis variants.' in out.getvalue()
    assert AxisVariantRefresh.objects.count() == 1
    call_command('refresh_axis_variants', '--dry-run', stdout=out)
    assert '1 source codes have more than one unit.' in out.getvalue()
