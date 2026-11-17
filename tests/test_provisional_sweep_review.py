"""The #2662 review round: pending keys, a cache outage, the task limits, the person lock.

Shares its fixtures and helpers with tests/test_provisional_sweep.py.
"""
import threading
import time
from datetime import date
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from omop_core import tasks
from omop_core.mapping.code_resolution import NO_MATCHING_CONCEPT_ID
from omop_core.models import Measurement, PatientSourceCode, SourceCodeResolveRun
from omop_core.services import provisional_sweep
from omop_core.services.derivation_jobs import use_dispatcher
from tests.factories import MeasurementFactory, PatientRecordFactory, PersonFactory
from tests.test_provisional_sweep import (  # noqa: F401 (fixtures)
    _clean_cache, _person, _proposal, _row, _warm, broker, world,
)

pytestmark = pytest.mark.django_db


# --- pending keys, the cache -----------------------------------------------------------------

def test_a_save_that_rolls_back_leaves_no_pending_key_to_swallow_the_next(broker, world,
                                                                         django_capture_on_commit_callbacks):
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            _proposal(world)
            raise RuntimeError('the request failed after the save')

    with django_capture_on_commit_callbacks(execute=True):
        mapping = _proposal(world)

    assert [a[2] for a in broker['sweep']] == [mapping.pk]


def test_re_pointing_a_qualifying_mapping_at_another_code_queues_a_sweep(broker, world,
                                                                        django_capture_on_commit_callbacks):
    mapping = _proposal(world)
    broker['sweep'].clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        mapping.source_code = 'GLU-2'
        mapping.save()

    assert [a[2] for a in broker['sweep']] == [mapping.pk]


def test_a_save_that_cannot_matter_skips_the_before_lookup(broker, world):
    mapping = _proposal(world)

    with CaptureQueriesContext(connection) as queries:
        mapping.notes = 'a note'
        mapping.save(update_fields=['notes'])

    assert not any(q['sql'].lstrip().upper().startswith('SELECT') for q in queries)


class _BrokenCache:
    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError('cache down')
        return fail


def test_a_cache_outage_never_fails_a_save_or_an_ingest_and_still_queues(broker, world, monkeypatch,
                                                                         django_capture_on_commit_callbacks):
    person = _person()
    _warm()
    monkeypatch.setattr(provisional_sweep, 'cache', _BrokenCache())

    with django_capture_on_commit_callbacks(execute=True):
        mapping = _proposal(world)                                # the save does not 500
        provisional_sweep.queue_person_resolve(person.person_id)  # nor does the ingest check

    assert [a[2] for a in broker['sweep']] == [mapping.pk]        # coalescing is lost, not the job


def test_a_second_proposal_within_the_refresh_window_is_not_hidden_by_a_stale_index(
        broker, world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _proposal(world, code='X-1')
    tasks.provisional_sweep_task.run(*broker['sweep'].pop())      # builds the index, opens the window
    with django_capture_on_commit_callbacks(execute=True):
        _proposal(world, code='Y-1')                              # 30 s later, inside the window
    tasks.provisional_sweep_task.run(*broker['sweep'].pop())      # skips the rebuild
    newcomer = _person(code='Y-1')                                # a new patient holding the new code

    with django_capture_on_commit_callbacks(execute=True):
        assert provisional_sweep.person_needs_resolve(newcomer.person_id) is None   # not "False" from a stale index
        provisional_sweep.queue_person_resolve(newcomer.person_id)

    assert broker['warm'] == [1]
    tasks.provisional_warm_index_task.run()
    assert provisional_sweep.person_needs_resolve(newcomer.person_id) is True


def test_a_partial_resolve_still_marks_the_record_stale_and_queues_the_derivation(world):
    from unittest import mock
    from omop_core.mapping import code_resolution
    from omop_core.models import PatientRecord
    from omop_core.services.derivation_jobs import use_dispatcher

    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientRecord.objects.filter(person=person).update(derivation_version=5)
    for code in ('A-1', 'B-1'):
        MeasurementFactory(person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
                           measurement_source_value=code, measurement_date=date(2026, 1, 1))
        _proposal(world, code=code)
    real = code_resolution._reconcile_with_proposal

    def fail_on_b(model, concept_col, source_col, table_key, rows, sv, *args, **kwargs):
        if sv == 'B-1':
            raise RuntimeError('group B failed')
        return real(model, concept_col, source_col, table_key, rows, sv, *args, **kwargs)

    class Recorder:
        dispatched = []

        def dispatch(self, person):
            self.dispatched.append(person.person_id)
            return 'task'

        def status(self, task_id):
            raise NotImplementedError

    recorder = Recorder()
    with mock.patch.object(code_resolution, '_reconcile_with_proposal', fail_on_b), use_dispatcher(recorder):
        with pytest.raises(RuntimeError, match='group B failed'):
            code_resolution.resolve_person_source_codes(person)

    assert Measurement.objects.get(person=person, measurement_source_value='A-1').confidence == 85   # committed
    assert PatientRecord.objects.get(person=person).derivation_version == 0                          # stale
    assert recorder.dispatched == [person.person_id]                                                 # and queued


def test_a_per_process_cache_is_warned_about_once(broker, world, caplog):
    provisional_sweep._warned.clear()

    with caplog.at_level('WARNING', logger=provisional_sweep.logger.name):
        provisional_sweep._take_pending('a')
        provisional_sweep._take_pending('b')

    assert sum('per-process' in record.message for record in caplog.records) == 1


# --- the task limits and the command -----------------------------------------------------------

def test_the_tasks_have_a_soft_limit_below_the_hard_one_and_a_spent_sweep_fails_its_run(world, settings):
    assert 0 < tasks.provisional_sweep_task.soft_time_limit < settings.CELERY_TASK_TIME_LIMIT
    assert 0 < tasks.provisional_resolve_person_task.soft_time_limit < settings.CELERY_TASK_TIME_LIMIT
    run = provisional_sweep.start_run_record()

    tasks.provisional_sweep_task.on_failure(
        RuntimeError('boom'), 'task-id', (str(run.id), 0, None, 200), {}, None)

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.FAILED and 'boom' in run.error_detail[0]['error']


def test_queue_without_a_broker_says_it_ran_inline(world):
    person = _person()
    _proposal(world)
    out = StringIO()

    call_command('sweep_provisional_rows', '--queue', stdout=out)

    assert 'running the sweep here instead' in out.getvalue() and 'Queued' not in out.getvalue()
    assert _row(person).confidence == 85


def test_the_no_broker_skip_is_logged_once_at_info(world, caplog):
    provisional_sweep._warned.clear()
    person = _person()

    with caplog.at_level('INFO', logger=provisional_sweep.logger.name):
        provisional_sweep.queue_person_resolve(person.person_id)
        provisional_sweep.queue_person_resolve(person.person_id)

    assert sum('no CELERY_BROKER_URL' in record.message for record in caplog.records) == 1


# --- the person lock and the derivation --------------------------------------------------------

def test_a_failing_derivation_does_not_roll_back_the_rows_it_follows(world):
    class Exploding:
        def dispatch(self, person):
            raise RuntimeError('derivation timed out')

        def status(self, task_id):
            raise NotImplementedError

    person = _person()
    _proposal(world)

    with use_dispatcher(Exploding()):
        with pytest.raises(RuntimeError):
            provisional_sweep.locked_resolve(person)

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (world.concept_id, 85)


@pytest.mark.django_db(transaction=True)
def test_the_resolve_runs_without_an_outer_transaction(world, monkeypatch):
    person = _person()
    _proposal(world)
    seen = []
    real = provisional_sweep.resolve_person_source_codes

    def spy(person, **kwargs):
        seen.append(connection.in_atomic_block)
        return real(person, **kwargs)
    monkeypatch.setattr(provisional_sweep, 'resolve_person_source_codes', spy)

    provisional_sweep.locked_resolve(person)

    assert seen == [False]                                        # no lock-holding transaction around the groups


@pytest.mark.django_db(transaction=True)
def test_a_person_another_worker_holds_is_skipped_not_waited_on_for_long(world, monkeypatch):
    person = _person()
    _proposal(world)
    monkeypatch.setattr(provisional_sweep, 'LOCK_WAIT_SECONDS', 0.3)
    assert provisional_sweep._try_lock(person.person_id)          # this session holds it
    outcome = {}

    def other_worker():
        try:
            provisional_sweep.locked_resolve(person)
            outcome['result'] = 'resolved'
        except provisional_sweep.PersonBusy:
            outcome['result'] = 'busy'
        finally:
            connection.close()

    thread = threading.Thread(target=other_worker)
    thread.start()
    thread.join(10)
    provisional_sweep._unlock(person.person_id)

    assert outcome['result'] == 'busy'
    assert _row(person).confidence is None                        # nothing was written meanwhile
    provisional_sweep.locked_resolve(person)                      # free again
    assert _row(person).confidence == 85


@pytest.mark.django_db(transaction=True)
def test_a_resolve_does_not_deadlock_with_a_writer_that_locks_rows_in_the_other_order(world):
    """The ETL's keyed upsert locks the rows it touches; the resolve must not hold one
    group's rows while it waits for another's. B locks the second code's row first, then
    wants the first code's: it can only get it if the resolve has already committed that group."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    first = MeasurementFactory(person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
                               measurement_source_value='A-1', measurement_date=date(2026, 1, 1))
    second = MeasurementFactory(person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
                                measurement_source_value='B-1', measurement_date=date(2026, 1, 1))
    for code in ('A-1', 'B-1'):
        PatientSourceCode.objects.create(person=person, source_value=code, omop_table='measurement',
                                         source_vocabulary_id='Local')
        _proposal(world, code=code)
    holding, outcome = threading.Event(), {}

    def etl_writer():
        try:
            with transaction.atomic():
                list(Measurement.objects.select_for_update().filter(pk=second.pk))
                holding.set()
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if Measurement.objects.get(pk=first.pk).confidence == 85:
                        break
                    time.sleep(0.05)
                else:
                    outcome['error'] = 'the first group was never committed'
                    return
                list(Measurement.objects.select_for_update().filter(pk=first.pk))   # would deadlock
            outcome['ok'] = True
        except Exception as exc:  # noqa: BLE001
            outcome['error'] = repr(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=etl_writer)
    thread.start()
    assert holding.wait(5)

    provisional_sweep.locked_resolve(person)                      # group B waits for the writer to finish
    thread.join(15)

    assert outcome == {'ok': True}
    assert {m.measurement_source_value: m.confidence for m in Measurement.objects.filter(person=person)} == {
        'A-1': 85, 'B-1': 85}
