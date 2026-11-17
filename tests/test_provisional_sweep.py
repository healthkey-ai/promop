"""Provisional rows for patients whose codes have a proposal (#2662).

The ETL does not propose concepts; Code to Concept and Concept to Code do, and
store the ranker's confidence on the mapping. The sweep, the mapping trigger and
the after-ingest job make a patient's unresolved rows provisional from such a
proposal without any ETL change. The sweep is the manual path (no broker); the
trigger and the ingest hook queue Celery jobs and never resolve in a request.
"""
from datetime import date

import pytest
from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext
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
