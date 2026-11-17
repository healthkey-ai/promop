"""Provisional rows for patients whose codes have a proposal (#2662).

The ETL does not propose concepts; Code to Concept and Concept to Code do, and
store the ranker's confidence on the mapping. The sweep, the mapping trigger and
the after-ingest job make a patient's unresolved rows provisional from such a
proposal without any ETL change.
"""
from datetime import date

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from omop_core.mapping.code_resolution import (
    NO_MATCHING_CONCEPT_ID, promote_provisional_rows, resolve_person_source_codes,
    revert_provisional_rows,
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


@pytest.fixture()
def world(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.PROVISIONAL_CONFIDENCE_FLOOR = 70
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=NO_MATCHING_CONCEPT_ID, concept_code='0',
                   concept_name='No matching concept', standard_concept=None)
    ConceptFactory(concept_id=32817, concept_code='32817', concept_name='EHR')
    return ConceptFactory(concept_name='Proposed glucose')


def _person(with_row=True, code=CODE):
    """A person holding the code unresolved (the way an ETL load leaves it)."""
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=code, omop_table='measurement', source_vocabulary_id='Local')
    if with_row:
        MeasurementFactory(
            person=person, measurement_concept_id=NO_MATCHING_CONCEPT_ID,
            measurement_source_value=code, measurement_date=date(2026, 1, 1), confidence=None)
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


# --- the sweep -------------------------------------------------------------------------

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


# --- approval and rejection after a sweep ------------------------------------------------

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


# --- the trigger -----------------------------------------------------------------------

def test_saving_a_proposal_resolves_the_persons_holding_its_code(world, django_capture_on_commit_callbacks):
    person = _person()

    with django_capture_on_commit_callbacks(execute=True):
        _proposal(world)

    row = _row(person)
    assert (row.measurement_concept_id, row.confidence) == (world.concept_id, 85)


def test_a_change_of_confidence_requeues_and_an_unrelated_save_does_not(world, monkeypatch,
                                                                       django_capture_on_commit_callbacks):
    queued = []
    monkeypatch.setattr(provisional_sweep, 'dispatch_mapping_sweep', queued.append)
    mapping = _proposal(world, confidence=None)

    with django_capture_on_commit_callbacks(execute=True):
        mapping.suggestion_confidence = 0.9
        mapping.save(update_fields=['suggestion_confidence'])
        mapping.notes = 'curator note'
        mapping.save(update_fields=['notes'])

    assert queued == [mapping.pk]


def test_approved_rejected_scoped_and_switched_off_saves_queue_nothing(world, settings, monkeypatch,
                                                                      django_capture_on_commit_callbacks):
    queued = []
    monkeypatch.setattr(provisional_sweep, 'dispatch_mapping_sweep', queued.append)

    with django_capture_on_commit_callbacks(execute=True):
        _proposal(world, status='approved', code='A')
        _proposal(world, status='rejected', code='B')
        _proposal(world, organization=OrganizationFactory(), code='C')
        settings.PROVISIONAL_DATA_ENABLED = False
        _proposal(world, code='D')

    assert queued == []


def test_with_a_broker_the_trigger_queues_a_task_not_inline_work(world, settings, monkeypatch,
                                                                 django_capture_on_commit_callbacks):
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'
    delayed = []
    from omop_core import tasks
    monkeypatch.setattr(tasks.provisional_sweep_task, 'delay', lambda *a: delayed.append(a))
    person = _person()

    with django_capture_on_commit_callbacks(execute=True):
        mapping = _proposal(world)

    assert [a[1:3] for a in delayed] == [(0, mapping.pk)]
    assert _row(person).confidence is None            # nothing ran inline


def test_a_chunk_chains_the_next_with_its_cursor(world, settings, monkeypatch):
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'
    from omop_core import tasks
    chained = []
    monkeypatch.setattr(tasks.provisional_sweep_task, 'delay', lambda *a: chained.append(a))
    people = sorted((_person() for _ in range(3)), key=lambda p: p.person_id)
    _proposal(world)
    run = provisional_sweep.start_run_record()

    tasks.provisional_sweep_task.run(str(run.id), 0, None, 2)

    assert chained == [(str(run.id), people[1].person_id, None, 2)]


# --- after ingest ------------------------------------------------------------------------

def test_an_ingested_person_is_resolved_when_a_proposal_covers_their_code(world,
                                                                         django_capture_on_commit_callbacks):
    person = _person()
    _proposal(world)

    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)

    assert _row(person).confidence == 85


def test_a_person_no_proposal_covers_is_not_queued(world, monkeypatch, django_capture_on_commit_callbacks):
    called = []
    monkeypatch.setattr(provisional_sweep, 'dispatch_person_resolve', called.append)
    person = _person(code='OTHER')
    _proposal(world)

    with django_capture_on_commit_callbacks(execute=True):
        provisional_sweep.queue_person_resolve(person.person_id)

    assert called == []


def test_the_ingest_check_costs_nothing_with_the_switch_off(world, settings):
    person = _person()
    settings.PROVISIONAL_DATA_ENABLED = False

    with CaptureQueriesContext(connection) as queries:
        provisional_sweep.queue_person_resolve(person.person_id)

    assert len(queries) == 0


def test_a_bulk_write_makes_the_new_rows_provisional(world, django_capture_on_commit_callbacks):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    PatientSourceCode.objects.create(
        person=person, source_value=CODE, omop_table='measurement', source_vocabulary_id='Local')
    _proposal(world)
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='sweep-etl@example.org', password='test', is_staff=True))
    row = {
        'person': person.person_id, 'measurement_concept': NO_MATCHING_CONCEPT_ID,
        'measurement_date': '2026-11-01', 'measurement_datetime': '2026-11-01T08:00:00Z',
        'measurement_type_concept': 32817, 'measurement_source_value': CODE,
        'value_as_number': '5.1', 'value_source_value': '5.1', 'unit_source_value': 'mmol/L',
    }

    with django_capture_on_commit_callbacks(execute=True):
        response = client.post('/api/v1/measurements/?skip_refresh=true', [row], format='json')

    assert response.status_code == 201, response.content
    written = _rows(person).get()
    assert (written.measurement_concept_id, written.confidence) == (world.concept_id, 85)
