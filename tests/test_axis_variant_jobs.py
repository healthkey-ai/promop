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

from omop_core.models import (
    AxisVariantRefresh, PatientSourceCode, SourceCodeAxisVariant,
    SourceCodeConceptMapping,
)
from omop_core.mapping import axis_variants
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
                      'keys': 1, 'refreshed': 1, 'rebuild_evidence': False}
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
    from omop_core.mapping.axis_variants import key_text

    request = AxisVariantRefresh.objects.create(reason='requeued', done=1, refreshed=1, cursor=key_text((LAB, 'GLU')))
    result = axis_variant_jobs.run_refresh(request.pk, batch_size=1)
    request.refresh_from_db()
    two_unit_code.refresh_from_db()
    other.refresh_from_db()
    assert result['outcome'] == 'done' and result['keys'] == 2 and result['refreshed'] == 2
    assert (request.done, request.refreshed, request.cursor) == (2, 2, key_text((LAB, 'GLU2')))
    assert (two_unit_code.axis_variant_count, other.axis_variant_count) == (0, 2), \
        'the code up to the cursor was not walked again'


def test_a_resumed_rebuild_is_not_shifted_by_codes_the_first_attempt_cleared(two_unit_code):
    """A rebuild drops codes with no patient data from the key set as it goes;
    the resume follows the cursor, not an offset into the shrunken list."""
    from omop_core.mapping.axis_variants import key_text

    cleared = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='A-NOPAT', omop_table='measurement', source_unit_evidence=SEEDED)
    later = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='Z-LATER', omop_table='measurement', source_unit_evidence=SEEDED)
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='Z-LATER', source_vocabulary_id=LAB,
                                     omop_table='measurement', source_unit='%', occurrence_count=2)
    # The first attempt walked A-NOPAT (now cleared, so gone from the key set) and died.
    cleared.source_unit_evidence = []
    cleared.save(update_fields=['source_unit_evidence'])
    request = AxisVariantRefresh.objects.create(
        reason='requeued', rebuild_evidence=True, done=1, cursor=key_text((LAB, 'A-NOPAT')))
    result = axis_variant_jobs.run_refresh(request.pk, batch_size=1)
    assert result['keys'] == 3  # A-NOPAT done before, GLU and Z-LATER now
    two_unit_code.refresh_from_db()
    later.refresh_from_db()
    assert two_unit_code.axis_variant_count == 2
    assert [(e['display'], e['count'], e['patients']) for e in later.source_unit_evidence] == [('%', 2, 1)]


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


SEEDED = [{'display': 'g/dL', 'code': 'g/dL', 'count': 10, 'patients': 1, 'source': 'nikita-fhir_code_inventory_20261005b'},
          {'display': '%', 'code': '%', 'count': 1, 'patients': 1, 'source': 'nikita-fhir_code_inventory_20261005b'}]


def test_a_rebuild_request_rewrites_evidence_from_patient_data_and_refreshes(two_unit_code):
    """The seed's label-level counts (#2041) give way to what this instance's patients say (#2043)."""
    two_unit_code.source_unit_evidence = SEEDED
    two_unit_code.save(update_fields=['source_unit_evidence'])
    request = AxisVariantRefresh.objects.create(reason='test', rebuild_evidence=True)
    result = axis_variant_jobs.run_refresh(request.pk)
    assert result['outcome'] == 'done' and result['rebuild_evidence'] is True and result['keys'] == 1
    two_unit_code.refresh_from_db()
    assert two_unit_code.source_unit_evidence == [
        {'display': 'mg/dL', 'code': 'mg/dL', 'normalized': 'mg/dL', 'count': 3, 'patients': 1, 'source': 'patient-data'},
        {'display': 'mmol/L', 'code': 'mmol/L', 'normalized': 'mmol/L', 'count': 3, 'patients': 1, 'source': 'patient-data'},
    ]
    assert two_unit_code.axis_variant_count == 2
    assert sorted(v.units for v in two_unit_code.axis_variants.all()) == [['mg/dL'], ['mmol/L']]


def test_a_rebuild_clears_seeded_units_a_code_was_never_seen_in():
    from omop_core.mapping import axis_variants

    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='ALB', omop_table='measurement', source_unit_evidence=SEEDED)
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='ALB', source_vocabulary_id=LAB,
                                     omop_table='measurement', source_unit='g/dL', occurrence_count=1)
    axis_variants.refresh_all(rebuild_evidence=True)
    mapping.refresh_from_db()
    assert [(e['display'], e['count']) for e in mapping.source_unit_evidence] == [('g/dL', 1)]
    assert mapping.axis_variant_count == 0
    # A code with no patient data on this instance keeps no units at all.
    bare = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='NOPAT', omop_table='measurement', source_unit_evidence=SEEDED)
    assert (LAB, 'NOPAT') in axis_variants.evidence_keys()
    axis_variants.refresh_all(rebuild_evidence=True)
    bare.refresh_from_db()
    assert bare.source_unit_evidence == []


def test_a_rebuild_keeps_units_this_instance_recorded_itself_and_merges_spellings():
    """A text term's units come from lookups, not patients, and stay; two spellings are one unit and one person."""
    from omop_core.mapping import axis_variants

    term = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='', source_code='blood glucose', omop_table='measurement',
        source_unit_evidence=[{'code': 'mg/dL', 'display': 'mg/dL', 'count': 4}, {'code': 'mmol/L', 'display': 'mmol/L', 'count': 1}],
        axis_variant_count=2)
    code = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='HGB', omop_table='measurement', source_unit_evidence=SEEDED)
    # Two patients, two spellings of one unit: one unit, two people.
    for unit in ('g/dL', 'G/DL'):
        PatientSourceCode.objects.create(person=PersonFactory(), source_value='HGB', source_vocabulary_id=LAB,
                                         omop_table='measurement', source_unit=unit, occurrence_count=2)
    axis_variants.refresh_all(rebuild_evidence=True)
    term.refresh_from_db()
    code.refresh_from_db()
    assert [(e['code'], e['count']) for e in term.source_unit_evidence] == [('mg/dL', 4), ('mmol/L', 1)]
    assert term.axis_variant_count == 2
    assert code.source_unit_evidence == [
        {'display': 'g/dL', 'code': 'g/dL', 'normalized': 'g/dL', 'count': 4, 'patients': 2, 'source': 'patient-data'}]


def test_evidence_keys_ignore_patient_codes_with_no_mapping(two_unit_code):
    from omop_core.mapping import axis_variants

    PatientSourceCode.objects.create(person=PersonFactory(), source_value='UNMAPPED', source_vocabulary_id=LAB,
                                     omop_table='measurement', source_unit='mg/dL', occurrence_count=1)
    assert axis_variants.evidence_keys() == {(LAB, 'GLU')}


def test_a_rebuild_asked_for_while_a_refresh_is_queued_is_folded_into_it(settings):
    settings.CELERY_BROKER_URL = ''
    plain, _ = axis_variant_jobs.request_refresh(reason='plain')
    folded, created = axis_variant_jobs.request_refresh(reason='rebuild', rebuild_evidence=True)
    assert (folded.pk, created, folded.rebuild_evidence) == (plain.pk, False, True)
    assert AxisVariantRefresh.objects.get(pk=plain.pk).rebuild_evidence is True
    # A running request has read its flag already and is not changed.
    AxisVariantRefresh.objects.filter(pk=plain.pk).update(outcome='running', rebuild_evidence=False)
    running, created = axis_variant_jobs.request_refresh(reason='rebuild', rebuild_evidence=True)
    assert (running.pk, created, running.rebuild_evidence) == (plain.pk, False, False)
    out = StringIO()
    call_command('refresh_axis_variants', '--queue', '--rebuild-evidence', stdout=out)
    assert 'without the evidence rebuild' in out.getvalue()


def test_migration_0307_requests_one_rebuild(settings, two_unit_code):
    settings.CELERY_BROKER_URL = ''
    migration = importlib.import_module('omop_core.migrations.0307_unit_evidence_rebuild')
    migration.request_rebuild(apps, None)
    migration.request_rebuild(apps, None)
    request = AxisVariantRefresh.objects.get()
    assert (request.outcome, request.reason, request.rebuild_evidence) == ('queued', migration.REASON, True)
    two_unit_code.refresh_from_db()
    assert two_unit_code.axis_variant_count == 0, 'migrate must not perform the rebuild'
    # A plain refresh still queued takes the rebuild on instead of a second row.
    AxisVariantRefresh.objects.all().delete()
    plain = AxisVariantRefresh.objects.create(reason='plain')
    migration.request_rebuild(apps, None)
    plain.refresh_from_db()
    assert plain.rebuild_evidence is True and migration.REASON in plain.reason
    assert AxisVariantRefresh.objects.count() == 1


def test_the_command_rebuilds_evidence(two_unit_code):
    two_unit_code.source_unit_evidence = SEEDED
    two_unit_code.save(update_fields=['source_unit_evidence'])
    out = StringIO()
    call_command('refresh_axis_variants', '--dry-run', '--rebuild-evidence', stdout=out)
    assert '1 source codes would have their unit evidence rebuilt from patient data.' in out.getvalue()
    call_command('refresh_axis_variants', '--rebuild-evidence', stdout=out)
    assert '1 source codes had their unit evidence rebuilt from patient data.' in out.getvalue()
    two_unit_code.refresh_from_db()
    assert [e['display'] for e in two_unit_code.source_unit_evidence] == ['mg/dL', 'mmol/L']
    assert AxisVariantRefresh.objects.get().rebuild_evidence is True


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


def test_a_recheck_requeues_and_publishes_a_request_whose_worker_was_killed(settings):
    """The kill can come after the replacement worker booted (#2048)."""
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    stranded = AxisVariantRefresh.objects.create(
        reason='claimed by the instance Cloud Run shut down', outcome='running', rebuild_evidence=True,
        task_id='axis-variant-refresh-2', started_at=timezone.now() - timedelta(minutes=16),
        progress_at=timezone.now() - timedelta(minutes=16))
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes(republish=False) == 1
    publish.assert_called_once_with(args=[stranded.pk], task_id=f'axis-variant-refresh-{stranded.pk}')
    stranded.refresh_from_db()
    assert (stranded.outcome, stranded.rebuild_evidence) == ('queued', True)


def test_a_live_worker_keeps_rechecking_until_told_to_stop():
    import threading

    from omop_core import tasks

    stop = threading.Event()
    passes = []

    def dispatch():
        passes.append(1)
        if len(passes) == 2:
            stop.set()

    thread = tasks.keep_rechecking(dispatch, 0.01, stop=stop)
    thread.join(5)
    assert not thread.is_alive() and passes == [1, 1]
    assert thread.daemon, 'a recheck must not hold the worker open at shutdown'


def test_a_recheck_that_fails_is_logged_and_the_next_still_runs(caplog):
    import threading

    from omop_core import tasks

    stop = threading.Event()
    passes = []

    def dispatch():
        passes.append(1)
        if len(passes) == 1:
            raise RuntimeError('database away')
        stop.set()

    thread = tasks.keep_rechecking(dispatch, 0.01, stop=stop)
    thread.join(5)
    assert not thread.is_alive() and len(passes) == 2
    assert 'database away' in caplog.text


def test_the_recheck_interval_is_under_the_stranded_cutoff():
    from omop_core import tasks

    assert tasks.RECHECK_INTERVAL < axis_variant_jobs.STALE


def test_a_stranded_rebuild_is_carried_over_to_the_request_that_replaces_it(settings):
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    stranded = AxisVariantRefresh.objects.create(
        reason='worker died mid-rebuild', outcome='running', rebuild_evidence=True, cursor='A\x1f1',
        started_at=timezone.now() - timedelta(hours=1), progress_at=timezone.now() - timedelta(hours=1))
    waiting = AxisVariantRefresh.objects.create(reason='plain refresh asked for meanwhile')
    with patch(PUBLISH):
        assert axis_variant_jobs.dispatch_queued_refreshes() == 1
    stranded.refresh_from_db(); waiting.refresh_from_db()
    assert stranded.outcome == 'failed'
    assert waiting.rebuild_evidence, 'the rebuild the stranded request was asked for is not dropped'


def test_an_attempt_the_sweep_handed_on_writes_nothing_over_the_new_one(two_unit_code):
    """The sweep can requeue a walk still alive; its writes then land nowhere (#2048)."""
    request = AxisVariantRefresh.objects.create(reason='slow key collection')
    original = axis_variants.refresh_all

    def hand_on_then_walk(*args, **kwargs):
        # The sweep requeues this request and a second worker claims it.
        AxisVariantRefresh.objects.filter(pk=request.pk).update(
            outcome='running', started_at=timezone.now() + timedelta(seconds=1), done=7, cursor='Z\x1fZ')
        return original(*args, **kwargs)

    with patch.object(axis_variants, 'refresh_all', side_effect=hand_on_then_walk):
        assert axis_variant_jobs.run_refresh(request.pk)['outcome'] == 'done'
    request.refresh_from_db()
    assert (request.outcome, request.done, request.cursor) == ('running', 7, 'Z\x1fZ'), \
        'the first attempt finished its walk but wrote nothing over the second'


def test_a_rebuild_stores_the_axis_evidence_it_met_and_a_plain_refresh_does_not(two_unit_code):
    """The walk counts the axis evidence across the codes it rebuilds (#2051)."""
    rebuild = AxisVariantRefresh.objects.create(reason='rebuild', rebuild_evidence=True)
    assert axis_variant_jobs.run_refresh(rebuild.pk)['outcome'] == 'done'
    rebuild.refresh_from_db()
    assert rebuild.axis_evidence['codes'] == 1
    assert rebuild.axis_evidence['axes']['property']['varies'] == 1, 'mg/dL and mmol/L are two properties'
    assert rebuild.axis_evidence['axes']['scale_type']['bases'] == {'value_type': 1}
    assert rebuild.axis_evidence['axes']['system'] == {'settled': 0, 'hint': 0, 'varies': 0, 'bases': {}}

    plain = AxisVariantRefresh.objects.create(reason='plain')
    assert axis_variant_jobs.run_refresh(plain.pk)['outcome'] == 'done'
    plain.refresh_from_db()
    assert plain.axis_evidence == {}


def test_a_resumed_rebuild_continues_its_axis_evidence_counts(two_unit_code):
    stored = {'codes': 3, 'axes': {'property': {'settled': 2, 'hint': 1, 'varies': 1, 'bases': {'unit': 2}}}}
    request = AxisVariantRefresh.objects.create(
        reason='resumed', rebuild_evidence=True, done=3, cursor='A\x1fA', axis_evidence=stored)
    assert axis_variant_jobs.run_refresh(request.pk)['outcome'] == 'done'
    request.refresh_from_db()
    assert request.axis_evidence['codes'] == 4
    assert request.axis_evidence['axes']['property']['varies'] == 2


# --- Graceful requeue on worker shutdown ---


def test_worker_shutdown_requeues_a_running_refresh():
    """A dying worker moves its running request back to queued so the replacement resumes it instantly."""
    request = AxisVariantRefresh.objects.create(
        reason='mid-walk', outcome='running', rebuild_evidence=True,
        keys=40000, done=12500, refreshed=900, cursor='ACME-LAB\x1fGLU',
        started_at=timezone.now() - timedelta(hours=1),
        progress_at=timezone.now() - timedelta(seconds=30))
    assert axis_variant_jobs.requeue_on_shutdown() == 1
    request.refresh_from_db()
    assert (request.outcome, request.task_id, request.started_at) == ('queued', '', None)
    assert (request.done, request.refreshed, request.cursor) == (12500, 900, 'ACME-LAB\x1fGLU'), \
        'the progress survives the requeue'
    assert request.rebuild_evidence is True


def test_worker_shutdown_is_a_noop_when_nothing_is_running():
    AxisVariantRefresh.objects.create(reason='done', outcome='done')
    AxisVariantRefresh.objects.create(reason='queued')
    assert axis_variant_jobs.requeue_on_shutdown() == 0


def test_rebuild_evidence_counts_per_hospital_code():
    """Two hospital codes sharing the same source text get their own PSC counts, not the
    global Measurement aggregate that groups only on measurement_source_value."""
    from collections import Counter
    from datetime import date

    from omop_core.models import Measurement
    from tests.factories import ConceptFactory

    concept = ConceptFactory()
    person_a = PersonFactory()
    person_b = PersonFactory()
    # Two hospital codes that share source text 'ALB' (like albumin from two hospitals).
    hosp_a = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='HOSP-A', source_code='ALB', omop_table='measurement',
        source_unit_evidence=SEEDED)
    hosp_b = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='HOSP-B', source_code='ALB', omop_table='measurement',
        source_unit_evidence=SEEDED)
    # Hospital A: 100 g/dL from person_a, 5 mg/dL from person_b (unique constraint
    # is per person+source_value+vocab+table, so two units need two persons).
    PatientSourceCode.objects.create(
        person=person_a, source_value='ALB', source_vocabulary_id='HOSP-A',
        omop_table='measurement', source_unit='g/dL', occurrence_count=100)
    PatientSourceCode.objects.create(
        person=person_b, source_value='ALB', source_vocabulary_id='HOSP-A',
        omop_table='measurement', source_unit='mg/dL', occurrence_count=5)
    # Hospital B: 3 records in g/dL only.
    PatientSourceCode.objects.create(
        person=person_a, source_value='ALB', source_vocabulary_id='HOSP-B',
        omop_table='measurement', source_unit='g/dL', occurrence_count=3)
    # Measurement rows for 'ALB' — a large global total that must not leak into evidence.
    for i in range(50):
        Measurement.objects.create(
            measurement_id=70000 + i, person=person_a,
            measurement_concept=concept, measurement_date=date(2025, 1, 1),
            measurement_type_concept=concept,
            measurement_source_value='ALB', unit_source_value='g/dL')

    # The live path (unit_counts_for) must also be per-hospital, not the global
    # Measurement aggregate.
    live = axis_variants.unit_counts_for([hosp_a, hosp_b], include_imported=False)
    assert live[hosp_a.pk] == Counter({'g/dL': 100, 'mg/dL': 5}), \
        f'Live counts for Hospital A wrong: {live[hosp_a.pk]}'
    assert live[hosp_b.pk] == Counter({'g/dL': 3}), \
        f'Live counts for Hospital B wrong: {live[hosp_b.pk]}'

    # The rebuild path stores the same per-hospital evidence.
    axis_variants.rebuild_unit_evidence([hosp_a, hosp_b])
    hosp_a.refresh_from_db()
    hosp_b.refresh_from_db()
    a_by_unit = {e['display']: e['count'] for e in hosp_a.source_unit_evidence}
    assert a_by_unit == {'g/dL': 100, 'mg/dL': 5}, f'Hospital A evidence wrong: {a_by_unit}'
    b_by_unit = {e['display']: e['count'] for e in hosp_b.source_unit_evidence}
    assert b_by_unit == {'g/dL': 3}, f'Hospital B evidence wrong: {b_by_unit}'


def test_rebuild_replaces_stale_patient_data_entries():
    """A prior buggy walk stamped global Measurement counts as source: 'patient-data'.
    The rebuild must replace them with fresh PSC evidence, not concatenate old + new (#2057)."""
    stale_patient_data = [
        {'display': 'g/dL', 'code': 'g/dL', 'normalized': 'g/dL', 'count': 277000, 'patients': 500, 'source': 'patient-data'},
        {'display': 'g/L', 'code': 'g/L', 'normalized': 'g/L', 'count': 237, 'patients': 50, 'source': 'patient-data'},
    ]
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='ALB-STALE', omop_table='measurement',
        source_unit_evidence=stale_patient_data)
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='ALB-STALE', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='g/dL', occurrence_count=10)
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    assert len(mapping.source_unit_evidence) == 1, \
        f'Stale patient-data entries should be replaced, not kept: {mapping.source_unit_evidence}'
    assert mapping.source_unit_evidence[0]['display'] == 'g/dL'
    assert mapping.source_unit_evidence[0]['count'] == 10
    assert mapping.source_unit_evidence[0]['source'] == 'patient-data'


# --- FHIR codings extraction ---


def test_rebuild_stores_fhir_codings_from_patient_metadata():
    """Per-patient LOINC codings are copied onto the mapping's source_metadata."""
    from omop_core.models import LoincClass, LoincCodeClass

    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
    LoincCodeClass.objects.create(
        loinc_num='1751-7', loinc_class_id='CHEM', component='Albumin',
        property='MCnc', time_aspect='Pt', system='Ser/Plas', scale_type='Qn', method_type='')
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='ALB', omop_table='measurement',
        source_metadata={'record_count': 42})
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='ALB', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='g/dL', occurrence_count=5,
        source_metadata={'fhir': {'code': {'coding': [
            {'system': 'http://loinc.org', 'code': '1751-7', 'display': 'Albumin [Mass/volume] in Serum or Plasma'},
        ]}}})
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    codings = mapping.source_metadata.get('fhir', {}).get('code', {}).get('coding', [])
    assert len(codings) == 1
    assert codings[0]['code'] == '1751-7'
    assert codings[0]['system'] == 'http://loinc.org'


def test_rebuild_does_not_overwrite_existing_fhir_codings():
    """A mapping that already carries FHIR codings is left alone."""
    existing_fhir = {'code': {'coding': [
        {'system': 'http://loinc.org', 'code': '9999-9', 'display': 'Existing'}]}}
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='EXIST', omop_table='measurement',
        source_metadata={'fhir': existing_fhir})
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='EXIST', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='mg/dL', occurrence_count=3,
        source_metadata={'fhir': {'code': {'coding': [
            {'system': 'http://loinc.org', 'code': '1751-7', 'display': 'Albumin'},
        ]}}})
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    codings = mapping.source_metadata['fhir']['code']['coding']
    assert len(codings) == 1
    assert codings[0]['code'] == '9999-9', 'existing codings must not be overwritten'


def test_fhir_codings_enable_axis_settling():
    """After storing codings, loinc_axes.infer() settles axes from them."""
    from omop_core.mapping import loinc_axes
    from omop_core.models import LoincClass, LoincCodeClass

    LoincClass.objects.get_or_create(code='CHEM', defaults={'display_name': 'Chemistry'})
    LoincCodeClass.objects.create(
        loinc_num='1751-7', loinc_class_id='CHEM', component='Albumin',
        property='MCnc', time_aspect='Pt', system='Ser/Plas', scale_type='Qn', method_type='')
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='ALB2', omop_table='measurement',
        source_metadata={'record_count': 10})
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='ALB2', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='g/dL', occurrence_count=5,
        source_metadata={'fhir': {'code': {'coding': [
            {'system': 'http://loinc.org', 'code': '1751-7', 'display': 'Albumin [Mass/volume] in Serum or Plasma'},
        ]}}})
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    axes, _hints = loinc_axes.infer(
        mapping.source_vocabulary_id, mapping.source_code, mapping.source_metadata)
    assert axes.get('component', {}).get('value') == 'Albumin'
    assert axes.get('property', {}).get('value') == 'MCnc'
    assert axes.get('component', {}).get('basis') == loinc_axes.BASIS_FHIR_CODING


def test_the_boot_pass_publishes_a_request_the_shutdown_requeued(settings):
    """Shutdown → requeue → boot pass → publish: the full cycle."""
    settings.CELERY_BROKER_URL = 'redis://broker/0'
    request = AxisVariantRefresh.objects.create(
        reason='requeued by shutdown', outcome='queued', task_id='',
        done=5000, cursor='ACME-LAB\x1fGLU')
    with patch(PUBLISH) as publish:
        assert axis_variant_jobs.dispatch_queued_refreshes(republish=True) == 1
    publish.assert_called_once_with(args=[request.pk], task_id=f'axis-variant-refresh-{request.pk}')


def test_long_axis_key_round_trips():
    """axis_key longer than 255 chars stores and retrieves correctly (#2069)."""
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='LONG-KEY', omop_table='measurement')
    long_key = 'property=' + ','.join(f'Prop{i}' for i in range(80))
    assert len(long_key) > 255
    variant = SourceCodeAxisVariant.objects.create(
        mapping=mapping, axis_key=long_key, axis_values={}, units=['%'])
    variant.refresh_from_db()
    assert variant.axis_key == long_key


# --- occurrence_count rebuild (#2110) and variant count correction (#2111) ---


def test_rebuild_updates_occurrence_count_from_psc_totals():
    """rebuild_unit_evidence should update occurrence_count from PSC data (#2110).

    Hospital code seeds carried inflated label-level counts; the rebuild
    must correct them to the per-code PSC totals.
    """
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='ALB-COUNT', omop_table='measurement',
        occurrence_count=1600,  # inflated seed count
        source_unit_evidence=SEEDED)
    person = PersonFactory()
    PatientSourceCode.objects.create(
        person=person, source_value='ALB-COUNT', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='g/dL', occurrence_count=414)
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    assert mapping.occurrence_count == 414, \
        f'occurrence_count should be updated from PSC total, got {mapping.occurrence_count}'


def test_rebuild_does_not_zero_occurrence_count_when_no_psc_data():
    """A code with no PSC data should keep its existing occurrence_count (#2110)."""
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='NO-PSC', omop_table='measurement',
        occurrence_count=50,
        source_unit_evidence=SEEDED)
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    assert mapping.occurrence_count == 50, \
        'occurrence_count should not be zeroed when there is no PSC data'


def test_rebuild_sums_multiple_units_for_occurrence_count():
    """occurrence_count should be the sum across all units (#2110)."""
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU-SUM', omop_table='measurement',
        occurrence_count=1600,
        source_unit_evidence=SEEDED)
    person_a = PersonFactory()
    person_b = PersonFactory()
    PatientSourceCode.objects.create(
        person=person_a, source_value='GLU-SUM', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='mg/dL', occurrence_count=300)
    PatientSourceCode.objects.create(
        person=person_b, source_value='GLU-SUM', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='mmol/L', occurrence_count=114)
    axis_variants.rebuild_unit_evidence([mapping])
    mapping.refresh_from_db()
    assert mapping.occurrence_count == 414, \
        f'occurrence_count should be sum of all units, got {mapping.occurrence_count}'


def test_surviving_variant_rows_get_updated_counts_below_threshold():
    """Old variant rows with approval/destination should get corrected counts
    even when below FLAG_THRESHOLD (#2111)."""
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='ALB-VAR', omop_table='measurement',
        axis_variant_count=0)
    # Create an old variant row with inflated count and an approval
    old_variant = SourceCodeAxisVariant.objects.create(
        mapping=mapping, axis_key='property=MCnc|scale_type=Qn',
        axis_values={'property': 'MCnc', 'scale_type': 'Qn'},
        units=['g/dL'], occurrence_count=279045,
        status='approved', target_concept_id=12345)
    # Only one unit in PSC → 1 variant detected, below FLAG_THRESHOLD(2)
    person = PersonFactory()
    PatientSourceCode.objects.create(
        person=person, source_value='ALB-VAR', source_vocabulary_id=LAB,
        omop_table='measurement', source_unit='g/dL', occurrence_count=414)
    from collections import Counter
    counts = Counter({'g/dL': 414})
    axis_variants.refresh(mapping, counts)
    old_variant.refresh_from_db()
    assert old_variant.occurrence_count == 414, \
        f'Surviving variant should have corrected count, got {old_variant.occurrence_count}'


# --- Seen counts from this instance's patients, for every code (#2121) ---


def test_a_rebuild_sets_the_seen_count_from_this_instance_s_patients_per_coding_system():
    """The extract counted each coding of an observation, so two coding systems' codes showed one
    number (#2121); the per-patient rows give each its own, in one pass, walk or no walk."""
    order = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='urn:oid:1.2.3.2.7.2.768282', source_code='5090', omop_table='measurement',
        occurrence_count=1714, source_group_occurrence_count=1714)
    result = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='urn:oid:1.2.3.2.7.5.737384.134', source_code='5090', omop_table='measurement',
        occurrence_count=1714, source_group_occurrence_count=1714)
    never_loaded = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='urn:oid:1.2.3.2.7.2.768282', source_code='5091', omop_table='measurement',
        occurrence_count=99, source_group_occurrence_count=120)
    any_table = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='GLU', omop_table='', occurrence_count=1)
    for n in (3, 4):
        PatientSourceCode.objects.create(person=PersonFactory(), source_value='5090', occurrence_count=n,
                                         source_vocabulary_id=order.source_vocabulary_id, omop_table='measurement')
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='5090', occurrence_count=2,
                                     source_vocabulary_id=result.source_vocabulary_id, omop_table='measurement')
    # Recorded in another case than the mapping spells it: ingest resolves codes case-insensitively.
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='glu', occurrence_count=5,
                                     source_vocabulary_id=LAB, omop_table='measurement')
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='GLU', occurrence_count=6,
                                     source_vocabulary_id=LAB, omop_table='observation')
    # A row on another table does not count for a measurement mapping.
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='5090', occurrence_count=50,
                                     source_vocabulary_id=result.source_vocabulary_id, omop_table='observation')
    assert (order.source_vocabulary_id, '5090') not in axis_variants.evidence_keys(), \
        'a code with no unit is not walked; its count is set without a walk'
    assert axis_variants.rebuild_seen_counts() == 3
    for m in (order, result, never_loaded, any_table):
        m.refresh_from_db()
    assert (order.occurrence_count, result.occurrence_count) == (7, 2)
    assert (order.source_group_occurrence_count, result.source_group_occurrence_count) == (None, None), \
        "the extract's group figure no longer describes the members"
    assert (never_loaded.occurrence_count, never_loaded.source_group_occurrence_count) == (99, 120), \
        'a code with no patient rows here keeps its count'
    assert any_table.occurrence_count == 11, 'a mapping with no table counts every table'


def test_a_hospital_scoped_mapping_counts_its_own_patients_only():
    from tests.factories import OrganizationFactory, PatientRecordFactory

    ours, theirs = OrganizationFactory(), OrganizationFactory()
    shared = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='HGB', omop_table='measurement', occurrence_count=1)
    scoped = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code='HGB', omop_table='measurement', occurrence_count=1, organization=ours)
    for org, n in ((ours, 3), (theirs, 700)):
        record = PatientRecordFactory(organization=org)
        PatientSourceCode.objects.create(person=record.person, source_value='HGB', source_vocabulary_id=LAB,
                                         omop_table='measurement', source_unit='g/dL', occurrence_count=n)
    axis_variants.refresh_all(rebuild_evidence=True)
    shared.refresh_from_db(); scoped.refresh_from_db()
    assert (shared.occurrence_count, scoped.occurrence_count) == (703, 3)


def test_a_rebuild_request_sets_seen_counts_once_and_a_resumed_one_does_not_repeat_it(two_unit_code):
    SourceCodeConceptMapping.objects.filter(pk=two_unit_code.pk).update(occurrence_count=1714)
    request = AxisVariantRefresh.objects.create(reason='rebuild', rebuild_evidence=True)
    assert axis_variant_jobs.run_refresh(request.pk)['outcome'] == 'done'
    two_unit_code.refresh_from_db()
    assert two_unit_code.occurrence_count == 6, 'two patients, three records each'
    with patch.object(axis_variants, 'rebuild_seen_counts') as counts:
        resumed = AxisVariantRefresh.objects.create(reason='resumed', rebuild_evidence=True, done=1, cursor='A\x1fA')
        assert axis_variant_jobs.run_refresh(resumed.pk)['outcome'] == 'done'
        plain = AxisVariantRefresh.objects.create(reason='plain')
        assert axis_variant_jobs.run_refresh(plain.pk)['outcome'] == 'done'
    counts.assert_not_called()


def test_rebuilt_seen_keys_are_the_codes_on_patients_once_a_rebuild_completed(two_unit_code):
    bare = SourceCodeConceptMapping.objects.create(source_vocabulary_id=LAB, source_code='NOPAT', omop_table='measurement')
    assert axis_variants.rebuilt_seen_keys() == set(), 'nothing is protected before a rebuild'
    AxisVariantRefresh.objects.create(reason='done', rebuild_evidence=True, outcome='done')
    assert axis_variants.rebuilt_seen_keys() == {(LAB, 'GLU')}
    assert axis_variants.rebuilt_seen_keys([(LAB, 'GLU'), (LAB, 'NOPAT')]) == {(LAB, 'GLU')}
    assert axis_variants.rebuilt_seen_keys([(LAB, bare.source_code)]) == set()
