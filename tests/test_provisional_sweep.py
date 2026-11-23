"""Provisional rows for patients whose codes have a proposal (#2662).

The ETL does not propose concepts; Code to Concept and Concept to Code do, and
store the ranker's confidence on the mapping. The sweep, the mapping trigger and
the after-ingest job make a patient's unresolved rows provisional from such a
proposal without any ETL change. The sweep is the manual path (no broker); the
trigger and the ingest hook queue Celery jobs and never resolve in a request.
"""
from datetime import date, timedelta
from io import StringIO

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core import tasks
from omop_core.mapping.code_resolution import (
    NO_MATCHING_CONCEPT_ID, promote_provisional_rows, revert_provisional_rows,
)
from omop_core.models import (
    Measurement, PatientSourceCode, SourceCodeConceptMapping, SourceCodeResolveRun,
)
from omop_core.services import provisional_sweep
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, MeasurementFactory, OrganizationFactory, PatientRecordFactory, PersonFactory,
)

pytestmark = pytest.mark.django_db

CODE = 'GLU-1'
BROKER = 'redis://example.invalid/0'


@pytest.fixture(autouse=True)
def _clean_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture()
def world(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=32817, concept_code='32817', concept_name='EHR')
    return ConceptFactory(concept_name='Proposed glucose')


@pytest.fixture()
def broker(world, settings, monkeypatch):
    """A broker is set and every task `.delay` is captured instead of sent."""
    settings.CELERY_BROKER_URL = BROKER
    sent = {'sweep': [], 'person': [], 'warm': []}
    monkeypatch.setattr(tasks.provisional_sweep_task, 'delay', lambda *a: sent['sweep'].append(a))
    monkeypatch.setattr(tasks.provisional_resolve_person_task, 'delay', lambda pid: sent['person'].append(pid))
    monkeypatch.setattr(tasks.provisional_warm_index_task, 'delay', lambda: sent['warm'].append(1))
    return sent


def _warm():
    """What the worker does before the ingest check can answer: build the qualifying index."""
    provisional_sweep.qualifying_index(refresh=True)


def _person(with_row=True, code=CODE, erroneous=False):
    """A person holding the code unresolved (the way an ETL load leaves it)."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
    if with_row:
        MeasurementFactory(
            person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
            measurement_source_value=code, measurement_date=date(2026, 1, 1), confidence=None,
            is_erroneous=erroneous)
    return person


def _proposal(target, status='proposed', confidence=0.85, organization=None, code=CODE):
    return SourceCodeConceptMapping.objects.create(
        source_code=code, source_vocabulary_id='Local', source_code_description=code,
        domain_id='Measurement', omop_table='measurement', target_concept=target,
        status=status, suggestion_confidence=confidence, organization=organization)


def _rows(person, code=CODE):
    return Measurement.objects.filter(person=person, measurement_source_value=code)


def _row(person):
    return _rows(person).get()


# --- the sweep (the manual path) --------------------------------------------------------

def test_the_sweep_makes_rows_provisional_from_a_proposal_above_the_floor(world):
    person = _person()
    _proposal(world, confidence=0.85)

    run = provisional_sweep.start_sweep()

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (world.concept_id, 85)
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED
    assert run.provisional == 1 and run.done == 1 and run.errors == 0


def test_a_person_with_only_an_inventory_code_gets_a_row_made(world):
    person = _person(with_row=False)
    _proposal(world)

    provisional_sweep.start_sweep()

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (world.concept_id, 85)


def test_nothing_below_the_floor_approved_rejected_or_unmapped_is_made_provisional(world):
    below, approved, rejected, unmapped = (_person(code=c) for c in ('LOW', 'APP', 'REJ', 'NONE'))
    _proposal(world, confidence=0.5, code='LOW')
    _proposal(world, status='approved', code='APP')
    _proposal(world, status='rejected', code='REJ')

    provisional_sweep.start_sweep()

    for person, code in ((below, 'LOW'), (approved, 'APP'), (rejected, 'REJ'), (unmapped, 'NONE')):
        row = _rows(person, code).get()
        assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None), code


def test_a_proposal_the_resolver_would_reject_selects_no_candidates(world):
    _person()
    unusable = ConceptFactory(concept_name='Non-standard glucose', standard_concept=None)
    _proposal(unusable)

    assert provisional_sweep.qualifying_codes() == {}
    assert provisional_sweep.candidate_person_ids(include_provisional_holders=False) == []


def test_the_sweep_is_idempotent(world):
    person = _person()
    _proposal(world)
    provisional_sweep.start_sweep()
    before = list(_rows(person).values_list('pk', 'measurement_concept_id', 'confidence'))

    again = provisional_sweep.start_sweep()

    assert list(_rows(person).values_list('pk', 'measurement_concept_id', 'confidence')) == before
    again.refresh_from_db()
    assert again.provisional == 0 and again.errors == 0


def test_a_killed_sweep_resumes_from_its_cursor(world):
    people = sorted((_person() for _ in range(3)), key=lambda p: p.person_id)
    _proposal(world)
    run = provisional_sweep.start_run_record()

    first = provisional_sweep.run_sweep(run.id, after=0, limit=2)    # the task is killed after this

    assert first == people[1].person_id                              # the last person it finished
    assert not _rows(people[2]).filter(confidence__isnull=False).exists()
    second = provisional_sweep.run_sweep(run.id, after=first, limit=2)
    assert second is None
    assert all(_row(person).confidence == 85 for person in people)
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED and run.done == 3


def test_a_chunk_stops_on_its_time_budget_and_hands_on_its_cursor(world):
    people = sorted((_person() for _ in range(3)), key=lambda p: p.person_id)
    _proposal(world)
    run = provisional_sweep.start_run_record()

    cursor = provisional_sweep.run_sweep(run.id, after=0, limit=10, budget_seconds=-1)

    assert cursor == people[0].person_id                             # one person, then it yields
    run.refresh_from_db()
    assert run.done == 1 and run.state == SourceCodeResolveRun.RUNNING


def test_a_soft_time_limit_saves_progress_and_returns_the_cursor(world, monkeypatch):
    people = sorted((_person() for _ in range(3)), key=lambda p: p.person_id)
    _proposal(world)
    run = provisional_sweep.start_run_record()
    real = provisional_sweep.locked_resolve
    calls = []

    def resolve(person, **kwargs):
        calls.append(person.person_id)
        if len(calls) == 2:
            raise SoftTimeLimitExceeded()
        return real(person, **kwargs)
    monkeypatch.setattr(provisional_sweep, 'locked_resolve', resolve)

    cursor = provisional_sweep.run_sweep(run.id, after=0, limit=10)

    assert cursor == people[0].person_id
    run.refresh_from_db()
    assert run.done == 1 and run.provisional == 1 and run.errors == 0
    assert run.state == SourceCodeResolveRun.RUNNING                 # not failed: the next chunk goes on


def test_an_organization_scoped_proposal_is_left_to_its_own_hospital(world):
    person = _person()
    _proposal(world, organization=OrganizationFactory())

    provisional_sweep.start_sweep()

    # The per-patient inventory cannot say which hospital a code came from, so a
    # proposal scoped to one never writes provisional rows (provisional_proposal).
    assert _row(person).confidence is None


def test_with_the_switch_off_the_sweep_writes_nothing(world, settings):
    person = _person()
    _proposal(world)
    settings.PROVISIONAL_DATA_ENABLED = False

    assert provisional_sweep.start_sweep() is None
    run = provisional_sweep.start_run_record()
    assert provisional_sweep.run_sweep(run.id) is None

    assert (_row(person).measurement_concept_id, _row(person).confidence) == (NO_MATCHING_CONCEPT_ID, None)
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED and run.provisional == 0


def test_a_proposal_that_stopped_qualifying_gets_its_rows_taken_back(world):
    person = _person()
    mapping = _proposal(world)
    provisional_sweep.start_sweep()
    assert _row(person).confidence == 85
    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(suggestion_confidence=0.4)  # no hook fires

    provisional_sweep.start_sweep()

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)


def test_a_row_entered_in_error_is_never_made_provisional(world):
    person = _person(erroneous=True)
    _proposal(world)

    provisional_sweep.start_sweep()

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)


def test_the_resolver_itself_leaves_an_erroneous_row_alone(world):
    from omop_core.mapping.code_resolution import resolve_person_source_codes
    person = _person(erroneous=True)
    _proposal(world)

    resolve_person_source_codes(person)

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)


def test_candidates_cost_a_fixed_number_of_queries(world):
    _proposal(world)
    for _ in range(2):
        _person()
    with CaptureQueriesContext(connection) as small:
        provisional_sweep.candidate_person_ids(limit=500)
    for _ in range(6):
        _person()
    with CaptureQueriesContext(connection) as large:
        provisional_sweep.candidate_person_ids(limit=500)

    assert len(large) == len(small)


def test_two_resolves_of_one_person_back_to_back_make_one_row(world):
    person = _person(with_row=False)
    _proposal(world)
    person = type(person).objects.get(pk=person.pk)

    provisional_sweep.locked_resolve(person)
    provisional_sweep.locked_resolve(person)

    assert _rows(person).count() == 1


def test_a_resolve_takes_the_person_lock_first(world):
    person = _person()
    _proposal(world)

    with CaptureQueriesContext(connection) as queries:
        provisional_sweep.locked_resolve(person)

    sql = [q['sql'] for q in queries]
    assert 'pg_try_advisory_lock' in sql[0]                      # before any read of the person's rows
    assert 'pg_advisory_unlock' in sql[-1]                       # and released after the resolve


def test_approval_after_a_sweep_promotes_the_rows(world):
    person = _person()
    mapping = _proposal(world)
    provisional_sweep.start_sweep()
    assert _row(person).confidence == 85
    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(status='approved')
    mapping.refresh_from_db()

    promote_provisional_rows(mapping=mapping)

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (world.concept_id, None)


def test_rejection_after_a_sweep_reverts_the_rows(world):
    person = _person()
    mapping = _proposal(world)
    provisional_sweep.start_sweep()

    revert_provisional_rows(mapping=mapping, concept_id=world.concept_id)

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)


def test_a_real_row_is_never_touched(world):
    person = _person()
    real = ConceptFactory(concept_name='Approved glucose')
    MeasurementFactory(person=person, measurement_concept=real, measurement_source_value='OTHER',
                       measurement_date=date(2026, 1, 1), confidence=None)
    _proposal(world)

    provisional_sweep.start_sweep()

    assert _rows(person, 'OTHER').get().measurement_concept_id == real.concept_id


def test_a_failed_enqueue_marks_the_run_failed_not_pending(broker, monkeypatch, django_capture_on_commit_callbacks):
    def boom(*args):
        raise RuntimeError('queue down')
    monkeypatch.setattr(tasks.provisional_sweep_task, 'delay', boom)

    with django_capture_on_commit_callbacks(execute=True):
        run = provisional_sweep.start_sweep()

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.FAILED
    assert 'queue down' in run.error_detail[0]['error']


def test_a_chunk_chains_the_next_with_its_cursor(broker, monkeypatch):
    chained = []
    monkeypatch.setattr(tasks.provisional_sweep_task, 'delay', lambda *a: chained.append(a))
    world_concept = ConceptFactory(concept_name='Chain glucose')
    people = sorted((_person() for _ in range(3)), key=lambda p: p.person_id)
    _proposal(world_concept)
    run = provisional_sweep.start_run_record()

    tasks.provisional_sweep_task.run(str(run.id), 0, None, 2)

    assert chained == [(str(run.id), people[1].person_id, None, 2)]


# --- the trigger (broker only, coalesced, take-backs) -----------------------------------------

def test_saving_a_qualifying_proposal_queues_one_sweep_of_its_code(broker, world,
                                                                  django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        mapping = _proposal(world)

    assert [(a[1], a[2]) for a in broker['sweep']] == [(0, mapping.pk)]


def test_a_proposal_with_no_ranker_confidence_queues_nothing(broker, world, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _proposal(world, confidence=None)
        _proposal(world, confidence=0.3, code='LOW')

    assert broker['sweep'] == []


def test_a_notes_only_save_queues_nothing(broker, world, django_capture_on_commit_callbacks):
    mapping = _proposal(world)
    broker['sweep'].clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        mapping.notes = 'curator note'
        mapping.save()

    assert broker['sweep'] == []


def test_a_change_of_confidence_requeues(broker, world, django_capture_on_commit_callbacks):
    mapping = _proposal(world)
    broker['sweep'].clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        mapping.suggestion_confidence = 0.95
        mapping.save(update_fields=['suggestion_confidence'])

    assert len(broker['sweep']) == 1


def test_a_withdrawn_proposal_queues_its_take_back(broker, world, django_capture_on_commit_callbacks):
    rejected, cleared, lowered = _proposal(world, code='R'), _proposal(world, code='C'), _proposal(world, code='L')
    broker['sweep'].clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        rejected.status = 'rejected'
        rejected.save()
        cleared.target_concept = None
        cleared.save()
        lowered.suggestion_confidence = 0.4
        lowered.save()

    assert sorted(a[2] for a in broker['sweep']) == sorted([rejected.pk, cleared.pk, lowered.pk])


def test_a_withdrawn_proposal_gets_its_rows_back_when_the_sweep_runs(broker, world,
                                                                    django_capture_on_commit_callbacks):
    person = _person()
    mapping = _proposal(world)
    provisional_sweep.locked_resolve(person)
    assert _row(person).confidence == 85
    broker['sweep'].clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        mapping.status = 'rejected'
        mapping.save()
    run_id, after, mapping_id, limit = broker['sweep'][0]
    tasks.provisional_sweep_task.run(run_id, after, mapping_id, limit)

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)


def test_saves_for_one_code_before_the_sweep_runs_queue_one_sweep(broker, world, django_capture_on_commit_callbacks):
    mapping = _proposal(world)
    broker['sweep'].clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        mapping.suggestion_confidence = 0.9
        mapping.save()
        mapping.suggestion_confidence = 0.95
        mapping.save()
        mapping.suggestion_confidence = 0.99
        mapping.save()

    assert len(broker['sweep']) == 1


def test_a_change_after_the_sweep_has_started_queues_another(broker, world, django_capture_on_commit_callbacks):
    person = _person()
    with django_capture_on_commit_callbacks(execute=True):
        mapping = _proposal(world)
    run_id, after, mapping_id, limit = broker['sweep'].pop()
    tasks.provisional_sweep_task.run(run_id, after, mapping_id, limit)      # the sweep starts and finishes
    assert _row(person).confidence == 85

    with django_capture_on_commit_callbacks(execute=True):
        mapping.suggestion_confidence = 0.95
        mapping.save()

    assert len(broker['sweep']) == 1


def test_approved_scoped_and_switched_off_saves_queue_nothing(broker, world, settings,
                                                             django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        _proposal(world, status='approved', code='A')
        _proposal(world, organization=OrganizationFactory(), code='C')
        settings.PROVISIONAL_DATA_ENABLED = False
        _proposal(world, code='D')

    assert broker['sweep'] == []


def test_with_no_broker_a_save_does_nothing_automatic(world, monkeypatch, django_capture_on_commit_callbacks):
    person = _person()
    ran = []
    monkeypatch.setattr(provisional_sweep, 'start_sweep', lambda **kw: ran.append(kw))

    with django_capture_on_commit_callbacks(execute=True) as callbacks:
        _proposal(world)

    assert callbacks == [] and ran == []
    assert _row(person).confidence is None                      # nothing resolved in the request


# --- after ingest ------------------------------------------------------------------------

def test_an_ingested_person_queues_one_resolve_when_a_proposal_covers_their_code(broker, world,
                                                                                django_capture_on_commit_callbacks):
    person = _person()
    _proposal(world)
    _warm()

    with django_capture_on_commit_callbacks(execute=True):
        for _ in range(20):                                      # twenty POSTs for one patient
            provisional_sweep.queue_person_resolve(person.person_id)

    assert broker['person'] == [person.person_id]


def test_a_later_write_queues_another_once_the_job_has_started(broker, world, django_capture_on_commit_callbacks):
    person = _person()
    _proposal(world)
    _warm()
    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)
    tasks.provisional_resolve_person_task.run(person.person_id)
    assert _row(person).confidence == 85
    MeasurementFactory(person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
                       measurement_source_value=CODE, measurement_date=date(2026, 2, 1), confidence=None)

    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)

    assert broker['person'] == [person.person_id, person.person_id]


def test_a_person_already_provisional_at_the_current_proposal_queues_nothing(broker, world,
                                                                            django_capture_on_commit_callbacks):
    person = _person()
    _proposal(world)
    provisional_sweep.locked_resolve(person)
    cache.clear()
    _warm()

    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)

    assert broker['person'] == []


def test_a_person_no_proposal_covers_is_not_queued(broker, world, django_capture_on_commit_callbacks):
    person = _person(code='OTHER')
    _proposal(world)
    _warm()

    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)

    assert broker['person'] == []


def test_a_proposal_the_resolver_rejects_is_not_queued_for(broker, world, django_capture_on_commit_callbacks):
    person = _person()
    _proposal(ConceptFactory(concept_name='Non-standard', standard_concept=None))
    _warm()

    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)

    assert broker['person'] == []


def test_a_cold_cache_queues_a_warm_up_and_never_builds_the_index_in_the_request(
        broker, world, django_capture_on_commit_callbacks):
    person = _person()
    _proposal(world)
    cache.clear()

    with CaptureQueriesContext(connection) as queries:
        with django_capture_on_commit_callbacks(execute=True):
            provisional_sweep.queue_person_resolve(person.person_id)
            provisional_sweep.queue_person_resolve(person.person_id)

    assert not any('source_code_concept_mapping' in q['sql'] for q in queries)   # no proposal loaded inline
    assert broker['warm'] == [1] and broker['person'] == []                      # one warm-up, no job yet
    tasks.provisional_warm_index_task.run()                                      # the worker builds it
    assert provisional_sweep.person_needs_resolve(person.person_id) is True


def test_the_check_is_a_fixed_handful_of_queries_whatever_the_number_of_proposals(broker, world):
    person = _person()
    for number in range(30):
        _proposal(world, code=f'PROP-{number}')
    _proposal(world)
    _warm()

    with CaptureQueriesContext(connection) as queries:
        assert provisional_sweep.person_needs_resolve(person.person_id) is True

    assert len(queries) <= 8                                    # one per clinical table plus the inventory
    assert not any('source_code_concept_mapping' in q['sql'] for q in queries)
    assert sum(len(q['sql']) for q in queries) < 20_000          # no 189 KB of SQL


def test_a_person_coded_in_another_system_is_not_covered_by_that_systems_proposal(broker, world):
    person = _person()
    PatientSourceCode.objects.filter(person=person).update(source_vocabulary_id='Cerner')
    _proposal(world)                                            # a proposal for the code under 'Local'
    _warm()

    assert provisional_sweep.person_needs_resolve(person.person_id) is False
    PatientSourceCode.objects.filter(person=person).update(source_vocabulary_id='Local')
    assert provisional_sweep.person_needs_resolve(person.person_id) is True


def test_a_code_in_another_case_is_found_by_the_check_the_selection_and_the_resolver(broker, world):
    person = _person(code='glu-1')
    _proposal(world, code='GLU-1')
    _warm()

    assert provisional_sweep.person_needs_resolve(person.person_id) is True
    assert provisional_sweep.candidate_person_ids(include_provisional_holders=False) == [person.person_id]
    provisional_sweep.locked_resolve(person)
    row = _rows(person, 'glu-1').get()
    assert (row.measurement_concept_id, row.confidence) == (world.concept_id, 85)


def test_the_ingest_check_costs_nothing_with_the_switch_off_or_no_broker(broker, world, settings):
    person = _person()
    settings.PROVISIONAL_DATA_ENABLED = False
    with CaptureQueriesContext(connection) as off:
        provisional_sweep.queue_person_resolve(person.person_id)
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''
    with CaptureQueriesContext(connection) as no_broker:
        provisional_sweep.queue_person_resolve(person.person_id)

    assert len(off) == 0 and len(no_broker) == 0


def _etl_client():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='sweep-etl@example.org', password='test', is_staff=True))
    return client


def _etl_row(person):
    return {
        'person': person.person_id, 'measurement_concept': NO_MATCHING_CONCEPT_ID,
        'measurement_date': '2026-11-01', 'measurement_datetime': '2026-11-01T08:00:00Z',
        'measurement_type_concept': 32817, 'measurement_source_value': CODE,
        'value_as_number': '5.1', 'value_source_value': '5.1', 'unit_source_value': 'mmol/L',
    }


def test_a_bulk_write_queues_the_resolve_and_a_replay_is_made_provisional_again(
        broker, world, django_capture_on_commit_callbacks):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='Local')
    _proposal(world)
    _warm()
    client = _etl_client()
    url = '/api/v1/measurements/?skip_refresh=true'

    with django_capture_on_commit_callbacks(execute=True):
        first = client.post(url, [_etl_row(person)], format='json')
    assert first.status_code == 201, first.content
    assert broker['person'] == [person.person_id]
    tasks.provisional_resolve_person_task.run(person.person_id)
    assert (_row(person).measurement_concept_id, _row(person).confidence) == (world.concept_id, 85)

    # The ETL re-posts the same row at concept 0: the upsert rewrites the
    # provisional row to unresolved, and the hook queues the resolve again.
    with django_capture_on_commit_callbacks(execute=True):
        replay = client.post(url, [_etl_row(person)], format='json')
    assert replay.status_code == 201, replay.content
    assert replay.json()['created'] == 0 and replay.json()['updated'] == 1
    assert (_row(person).measurement_concept_id, _row(person).confidence) == (NO_MATCHING_CONCEPT_ID, None)
    assert broker['person'] == [person.person_id, person.person_id]
    tasks.provisional_resolve_person_task.run(person.person_id)

    assert (_row(person).measurement_concept_id, _row(person).confidence) == (world.concept_id, 85)
    assert _rows(person).count() == 1


# --- the full sweep resolves only a person's qualifying and provisional codes (#2663) ---

def _unrelated_rows(person, count, concept=NO_MATCHING_CONCEPT_ID):
    """*count* rows of codes no proposal covers, the bulk of a loaded patient's chart."""
    from omop_core.services.pk import next_pk_batch
    from omop_core.models import Concept
    type_concept = Concept.objects.get(concept_id=32817)
    ids = next_pk_batch(Measurement, 'measurement_id', count)
    Measurement.objects.bulk_create([
        Measurement(
            measurement_id=pk, person=person, measurement_concept_id=concept,
            measurement_source_value=f'OTHER-{n}', measurement_date=date(2026, 1, 1),
            measurement_type_concept=type_concept)
        for n, pk in enumerate(ids)])


@pytest.fixture()
def resolver_calls(monkeypatch):
    """Every resolve the sweep asks for: the person and the arguments it gave."""
    real = provisional_sweep.locked_resolve
    calls = []

    def spy(person, **kwargs):
        calls.append((person.person_id, kwargs))
        return real(person, **kwargs)
    monkeypatch.setattr(provisional_sweep, 'locked_resolve', spy)
    return calls


def test_the_full_sweep_resolves_only_the_codes_a_proposal_covers(world, resolver_calls):
    person = _person()
    _unrelated_rows(person, 40)
    _proposal(world)

    run = provisional_sweep.start_sweep()

    assert resolver_calls == [(person.person_id, {
        'source_values': [CODE], 'omop_tables': ['measurement']})]
    assert _row(person).confidence == 85
    run.refresh_from_db()
    # The unrelated chart is never walked: not counted as skipped or already resolved.
    assert (run.skipped, run.already_resolved, run.provisional) == (0, 0, 1)
    assert Measurement.objects.filter(
        person=person, measurement_source_value__startswith='OTHER-',
        measurement_concept_id=NO_MATCHING_CONCEPT_ID, confidence__isnull=True).count() == 40


def test_the_scope_adds_a_provisional_code_whose_proposal_stopped_qualifying(world, resolver_calls):
    person = _person(code='OLD')
    old = _proposal(world, code='OLD')
    provisional_sweep.start_sweep()
    assert _rows(person, 'OLD').get().confidence == 85
    SourceCodeConceptMapping.objects.filter(pk=old.pk).update(suggestion_confidence=0.4)  # no hook fires
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='Local')
    _proposal(world)                                              # another code does qualify
    resolver_calls.clear()

    run = provisional_sweep.start_sweep()

    (_pid, kwargs), = resolver_calls
    assert kwargs == {'source_values': ['GLU-1', 'OLD'], 'omop_tables': ['measurement']}
    row = _rows(person, 'OLD').get()
    assert (row.measurement_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)
    assert _rows(person).get().confidence == 85                   # the inventory code got its row
    run.refresh_from_db()
    assert (run.provisional, run.reverted) == (1, 1)


def test_an_inventory_code_with_no_row_is_in_the_scope_and_gets_one(world, resolver_calls):
    person = _person(with_row=False)
    _unrelated_rows(person, 5)
    _proposal(world)

    provisional_sweep.start_sweep()

    assert resolver_calls == [(person.person_id, {
        'source_values': [CODE], 'omop_tables': ['measurement']})]
    assert _row(person).confidence == 85


def test_the_scope_is_each_persons_own_codes(world):
    holder = _person(code='held')                                 # stored in lower case
    stranger = _person(code='NOT-PROPOSED')
    _proposal(world, code='HELD')
    codes = provisional_sweep.qualifying_codes()

    assert provisional_sweep.person_scope(holder.person_id, codes) == (['held'], ['measurement'])
    assert provisional_sweep.person_scope(stranger.person_id, codes) == ([], [])


def test_a_candidate_with_nothing_to_resolve_never_reaches_the_resolver(world, resolver_calls, monkeypatch):
    person = _person(code='UNRELATED')
    _proposal(world)
    # A stale candidate: nothing about this person is a qualifying or provisional code.
    monkeypatch.setattr(provisional_sweep, 'candidate_person_ids', lambda **kw: [person.person_id])
    run = provisional_sweep.start_run_record()

    assert provisional_sweep.run_sweep(run.id) is None

    assert resolver_calls == []                                   # an empty list would mean "no limit"
    run.refresh_from_db()
    assert run.done == 1 and run.errors == 0 and run.state == SourceCodeResolveRun.COMPLETED
    assert _rows(person, 'UNRELATED').get().measurement_concept_id == NO_MATCHING_CONCEPT_ID


def test_a_mapping_sweep_still_resolves_its_one_code(world, resolver_calls):
    person = _person()
    _unrelated_rows(person, 5)
    mapping = _proposal(world)
    run = provisional_sweep.start_run_record()

    provisional_sweep.run_sweep(run.id, mapping_id=mapping.pk)

    assert resolver_calls == [(person.person_id, {
        'source_values': [CODE], 'omop_tables': ['measurement']})]
    assert _row(person).confidence == 85


def test_the_scope_costs_one_query_per_table_whatever_the_chart_or_the_qualifying_set(world):
    """The walk is gone, and no IN list of qualifying codes is sent either (#2663)."""
    bare, loaded = _person(), _person()
    _unrelated_rows(loaded, 2000)
    _proposal(world)
    big_set = {table: {f'Q{n}' for n in range(1500)} | {CODE}
               for table in provisional_sweep.CLINICAL_TABLES}

    counts = {}
    for name, person in (('bare', bare), ('loaded', loaded)):
        with CaptureQueriesContext(connection) as queries:
            scope = provisional_sweep.person_scope(person.person_id, big_set)
        counts[name] = len(queries)
        assert scope == ([CODE], ['measurement'])

    # 5 tables x (the person's distinct codes + the provisional holders) + the inventory.
    assert counts == {'bare': 11, 'loaded': 11}


def test_a_loaded_chart_resolves_in_the_queries_of_a_bare_one(world):
    bare, loaded = _person(), _person()
    _unrelated_rows(loaded, 2000)
    _proposal(world)
    codes = provisional_sweep.qualifying_codes()

    counts = {}
    for name, person in (('bare', bare), ('loaded', loaded)):
        source_values, omop_tables = provisional_sweep.person_scope(person.person_id, codes)
        with CaptureQueriesContext(connection) as queries:
            provisional_sweep.locked_resolve(
                person, source_values=source_values, omop_tables=omop_tables)
        counts[name] = len(queries)

    assert counts['loaded'] == counts['bare']
    assert _row(loaded).confidence == 85


def test_every_spelling_of_a_proposed_code_is_resolved(world, resolver_calls):
    """The resolver matches a proposal case-insensitively, and so must the scope."""
    person = _person(code='HGB')
    PatientSourceCode.objects.create(
        person=person, source_value='Hgb', omop_table='measurement', source_vocabulary_id='Local')
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
        measurement_source_value='Hgb', measurement_date=date(2026, 1, 1), confidence=None)
    _unrelated_rows(person, 10)
    _proposal(world, code='HGB')

    provisional_sweep.start_sweep()

    (_pid, kwargs), = resolver_calls
    assert kwargs == {'source_values': ['HGB', 'Hgb'], 'omop_tables': ['measurement']}
    for spelling in ('HGB', 'Hgb'):
        row = _rows(person, spelling).get()
        assert (row.measurement_concept_id, row.confidence) == (world.concept_id, 85), spelling


def test_a_mixed_case_inventory_code_with_no_row_is_in_scope_and_gets_one(world):
    # Candidate selection looks for the spellings an ETL is likely to write
    # (code, upper, lower), so a person holding only `Hgb` is not selected; once
    # selected for another code, the scope still covers every spelling.
    person = _person(code='Hgb', with_row=False)
    _proposal(world, code='HGB')
    codes = provisional_sweep.qualifying_codes()

    scope = provisional_sweep.person_scope(person.person_id, codes)
    provisional_sweep.locked_resolve(person, source_values=scope[0], omop_tables=scope[1])

    assert scope == (['Hgb'], ['measurement'])
    assert _rows(person, 'Hgb').get().confidence == 85


def _concept_in(domain, name):
    from tests.factories import DomainFactory
    return ConceptFactory(concept_name=name, domain=DomainFactory(domain_id=domain, domain_name=domain))


def _observation_person(code, with_row=True):
    from omop_core.models import Observation
    from tests.factories import ObservationFactory
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=code, omop_table='observation', source_vocabulary_id='Local')
    if with_row:
        ObservationFactory(
            person=person, observation_concept_id=NO_MATCHING_CONCEPT_ID,
            observation_source_value=code, observation_date=date(2026, 1, 1), confidence=None)
    return person, Observation.objects.filter(person=person, observation_source_value=code)


def _table_proposal(table, domain, target, code, confidence=0.85):
    return SourceCodeConceptMapping.objects.create(
        source_code=code, source_vocabulary_id='Local', source_code_description=code,
        domain_id=domain, omop_table=table, target_concept=target,
        status='proposed', suggestion_confidence=confidence)


def test_an_inventory_only_observation_code_is_in_scope_and_gets_a_row(world, resolver_calls):
    target = _concept_in('Observation', 'Smoking status')
    person, rows = _observation_person('SMK', with_row=False)
    _table_proposal('observation', 'Observation', target, 'SMK')

    provisional_sweep.start_sweep()

    assert resolver_calls == [(person.person_id, {
        'source_values': ['SMK'], 'omop_tables': ['observation']})]
    row = rows.get()
    assert (row.observation_concept_id, row.confidence) == (target.concept_id, 85)


def test_a_provisional_condition_whose_proposal_stopped_qualifying_is_in_scope_and_reverted(
        world, resolver_calls):
    from omop_core.models import ConditionOccurrence
    from tests.factories import ConditionOccurrenceFactory
    target = _concept_in('Condition', 'Anemia')
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value='ANEM', omop_table='condition', source_vocabulary_id='Local')
    ConditionOccurrenceFactory(
        person=person, condition_concept_id=NO_MATCHING_CONCEPT_ID,
        condition_source_value='ANEM', condition_start_date=date(2026, 1, 1))
    mapping = _table_proposal('condition', 'Condition', target, 'ANEM')
    provisional_sweep.start_sweep()
    rows = ConditionOccurrence.objects.filter(person=person, condition_source_value='ANEM')
    assert rows.get().confidence == 85
    SourceCodeConceptMapping.objects.filter(pk=mapping.pk).update(suggestion_confidence=0.4)  # no hook fires
    resolver_calls.clear()

    run = provisional_sweep.start_sweep()

    assert resolver_calls == [(person.person_id, {
        'source_values': ['ANEM'], 'omop_tables': ['condition']})]
    row = rows.get()
    assert (row.condition_concept_id, row.confidence) == (NO_MATCHING_CONCEPT_ID, None)
    run.refresh_from_db()
    assert run.reverted == 1


def test_a_person_holding_codes_in_two_tables_resolves_each_in_its_own_table(world, resolver_calls):
    target = _concept_in('Observation', 'Smoking status')
    person, obs_rows = _observation_person('SMK')
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='Local')
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
        measurement_source_value=CODE, measurement_date=date(2026, 1, 1), confidence=None)
    # A code each table holds but only the other table has a proposal for stays as it is.
    MeasurementFactory(
        person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
        measurement_source_value='SMK', measurement_date=date(2026, 1, 1), confidence=None)
    _proposal(world)
    _table_proposal('observation', 'Observation', target, 'SMK')

    provisional_sweep.start_sweep()

    (_pid, kwargs), = resolver_calls
    assert kwargs == {'source_values': [CODE, 'SMK'], 'omop_tables': ['measurement', 'observation']}
    assert _row(person).confidence == 85
    assert obs_rows.get().confidence == 85
    assert _rows(person, 'SMK').get().confidence is None            # no measurement proposal for SMK


def test_an_inventory_alias_for_a_table_is_folded_onto_its_canonical_key(world):
    from omop_core.models import ConditionOccurrence  # noqa: F401
    target = _concept_in('Condition', 'Anemia')
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value='ANEM', omop_table='condition_occurrence', source_vocabulary_id='Local')
    _table_proposal('condition', 'Condition', target, 'ANEM')

    assert provisional_sweep.person_scope(
        person.person_id, provisional_sweep.qualifying_codes()) == (['ANEM'], ['condition'])


# --- a run's state: finished, still going, or lost ------------------------------------------

def _age(run, minutes):
    """Make the run's last progress *minutes* old (a queryset update skips auto_now)."""
    then = timezone.now() - timedelta(minutes=minutes)
    SourceCodeResolveRun.objects.filter(pk=run.pk).update(updated_at=then, created_at=then)
    run.refresh_from_db()
    return run


def test_a_finished_inline_sweep_is_completed_with_its_counts_and_a_finish_time(world):
    _person(); _person()
    _proposal(world)

    run = provisional_sweep.start_sweep()

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED
    assert run.finished_at is not None and run.updated_at is not None
    assert (run.done, run.total, run.provisional) == (2, 2, 2)


def test_a_queued_sweep_is_completed_by_its_last_chunk_even_when_the_last_fills_the_limit(broker, monkeypatch):
    chained = []
    monkeypatch.setattr(tasks.provisional_sweep_task, 'delay', lambda *a: chained.append(a))
    people = sorted((_person() for _ in range(2)), key=lambda p: p.person_id)
    _proposal(ConceptFactory(concept_name='Queued glucose'))
    run = provisional_sweep.start_run_record()

    tasks.provisional_sweep_task.run(str(run.id), 0, None, 2)        # exactly `limit` persons
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.RUNNING and run.done == 2
    assert chained == [(str(run.id), people[1].person_id, None, 2)]

    tasks.provisional_sweep_task.run(*chained.pop())                 # the empty chunk that ends it
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED and run.finished_at is not None
    assert chained == []


def test_a_run_still_working_stays_running_and_is_not_stale(world):
    for _ in range(3):
        _person()
    _proposal(world)
    run = provisional_sweep.start_run_record()

    provisional_sweep.run_sweep(run.id, after=0, limit=10, budget_seconds=-1)

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.RUNNING and run.finished_at is None
    assert run.updated_at is not None                                 # every chunk stamps it
    assert list(provisional_sweep.stale_runs()) == []


def test_an_inline_sweep_that_crashes_is_marked_failed_not_left_running(world, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('database went away')
    monkeypatch.setattr(provisional_sweep, 'run_sweep', boom)

    with pytest.raises(RuntimeError):
        provisional_sweep.start_sweep()

    run = SourceCodeResolveRun.objects.get()
    assert run.state == SourceCodeResolveRun.FAILED and run.finished_at is not None
    assert 'database went away' in run.error_detail[0]['error']


def test_a_late_chunk_reopens_a_run_closed_as_stale_and_finishes_it(world):
    _person()
    _proposal(world)
    run = _age(provisional_sweep.start_run_record(), 600)
    provisional_sweep.close_stale_runs()
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.FAILED

    provisional_sweep.run_sweep(run.id, after=0, limit=10)           # the chain was alive after all

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED and run.done == 1


def test_close_stale_closes_only_pending_and_running_runs_with_no_recent_progress(world):
    lost = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING, done=120, total=120), 300)
    never_started = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.PENDING), 300)
    fresh = SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING)
    slow_but_alive = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 90)
    done_long_ago = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.COMPLETED), 900)

    closed = provisional_sweep.close_stale_runs()

    assert {run.pk for run in closed} == {lost.pk, never_started.pk}
    for run in (lost, never_started, fresh, slow_but_alive, done_long_ago):
        run.refresh_from_db()
    assert (lost.state, never_started.state) == (SourceCodeResolveRun.FAILED, SourceCodeResolveRun.FAILED)
    assert lost.finished_at is not None and 'Closed as stale' in lost.error_detail[0]['error']
    assert lost.done == 120                                           # what it had done is kept
    assert fresh.state == slow_but_alive.state == SourceCodeResolveRun.RUNNING
    assert done_long_ago.state == SourceCodeResolveRun.COMPLETED


def test_a_run_with_no_heartbeat_falls_back_to_its_creation_time(world):
    # Defensive: nothing writes a null (the migration stamped existing runs with its own time).
    old = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 400)
    SourceCodeResolveRun.objects.filter(pk=old.pk).update(updated_at=None)

    assert [run.pk for run in provisional_sweep.stale_runs()] == [old.pk]


def test_close_stale_takes_the_threshold_and_a_dry_run_changes_nothing(world):
    run = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 30)

    assert provisional_sweep.close_stale_runs() == []                  # default: two hours
    assert [r.pk for r in provisional_sweep.close_stale_runs(timedelta(minutes=10), dry_run=True)] == [run.pk]
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.RUNNING
    assert [r.pk for r in provisional_sweep.close_stale_runs(timedelta(minutes=10))] == [run.pk]
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.FAILED


def test_a_run_that_saved_progress_after_it_was_read_is_not_closed(world, monkeypatch):
    run = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 300)
    real = provisional_sweep.stale_runs
    calls = []

    def racing(older_than=None):
        queryset = real(older_than)
        if calls:                                                      # between the read and the close
            SourceCodeResolveRun.objects.filter(pk=run.pk).update(updated_at=timezone.now())
        calls.append(1)
        return queryset
    monkeypatch.setattr(provisional_sweep, 'stale_runs', racing)

    assert provisional_sweep.close_stale_runs() == []
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.RUNNING


def test_the_command_closes_stale_runs_without_the_switch_and_reports_them(world, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    run = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 300)
    out = StringIO()

    call_command('sweep_provisional_rows', '--close-stale', '--dry-run', stdout=out)
    assert 'Would close 1 stale run(s)' in out.getvalue() and str(run.pk) in out.getvalue()
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.RUNNING

    out = StringIO()
    call_command('sweep_provisional_rows', '--close-stale', '--stale-minutes', '60', stdout=out)
    assert 'Closed 1 stale run(s)' in out.getvalue()
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.FAILED


def test_a_bulk_resolve_stamps_the_heartbeat_too(world):
    from omop_core.services.resolve_jobs import run_bulk_resolve
    person = _person()
    run = SourceCodeResolveRun.objects.create(total=1)
    SourceCodeResolveRun.objects.filter(pk=run.pk).update(updated_at=None)

    run_bulk_resolve(run.id, [person.person_id])

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED and run.updated_at is not None


def test_the_command_lists_the_runs_it_closed_as_failed(world):
    run = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 300)
    out = StringIO()

    call_command('sweep_provisional_rows', '--close-stale', stdout=out)

    line = next(line for line in out.getvalue().splitlines() if str(run.pk) in line)
    assert ' failed ' in line and ' running ' not in line


@pytest.mark.parametrize('args', [
    ['--stale-minutes', '0'], ['--stale-minutes', '-5'],
    ['--limit', '10'], ['--mapping-id', '1'], ['--after-person-id', '5'], ['--queue'],
])
def test_close_stale_rejects_a_threshold_below_one_and_sweep_options_it_would_ignore(world, args):
    run = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.RUNNING), 300)

    with pytest.raises(CommandError):
        call_command('sweep_provisional_rows', '--close-stale', *args, stdout=StringIO())

    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.RUNNING                  # nothing was closed


@pytest.mark.parametrize('error', [RuntimeError('database went away'), KeyboardInterrupt()])
def test_the_command_marks_its_run_failed_when_it_crashes_or_is_interrupted(world, monkeypatch, error):
    def boom(*args, **kwargs):
        raise error
    monkeypatch.setattr(provisional_sweep, 'run_sweep', boom)

    with pytest.raises(type(error)):
        call_command('sweep_provisional_rows', stdout=StringIO())

    run = SourceCodeResolveRun.objects.get()
    assert run.state == SourceCodeResolveRun.FAILED and run.finished_at is not None
    assert repr(error) in run.error_detail[0]['error']


def test_a_bulk_resolve_that_starts_after_its_run_was_closed_reopens_and_completes_it(world, monkeypatch):
    from omop_core.services import resolve_jobs
    person = _person()
    run = _age(SourceCodeResolveRun.objects.create(state=SourceCodeResolveRun.PENDING, total=1), 300)
    provisional_sweep.close_stale_runs()
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.FAILED and run.finished_at is not None
    seen = []
    real = resolve_jobs.resolve_person_source_codes

    def resolve(p, **kwargs):
        seen.append(SourceCodeResolveRun.objects.values_list('state', 'finished_at').get(pk=run.pk))
        return real(p, **kwargs)
    monkeypatch.setattr(resolve_jobs, 'resolve_person_source_codes', resolve)

    resolve_jobs.run_bulk_resolve(run.id, [person.person_id])         # the task that was only delayed

    assert seen == [(SourceCodeResolveRun.RUNNING, None)]             # open while it works
    run.refresh_from_db()
    assert run.state == SourceCodeResolveRun.COMPLETED and run.done == 1
