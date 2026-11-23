"""A mapping save queues the stored-row re-point; a job moves the rows."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from omop_core.services import repoint_jobs
from omop_core.services.derivation_jobs import FakeDispatcher, use_dispatcher
from tests.factories import ConceptFactory, MeasurementFactory, OrganizationFactory

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


def test_a_save_that_lands_after_the_last_read_is_dispatched_again():
    old, new = ConceptFactory(), ConceptFactory()
    late = ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[old.pk])
    # The save commits between the run's last read and its unlock.
    with patch.object(repoint_jobs, '_advisory_unlock',
                         side_effect=lambda pk: SourceCodeConceptMapping.objects.filter(pk=pk).update(
                             deferred_repoint_concept_ids=[late.pk])), \
            patch.object(repoint_jobs, 'dispatch_repoint') as dispatch:
        repoint_jobs.run_repoint(row.pk)
    dispatch.assert_called_once_with(row.pk)


def test_a_finished_run_does_not_dispatch_itself_again():
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[old.pk])
    with patch.object(repoint_jobs, 'dispatch_repoint') as dispatch:
        repoint_jobs.run_repoint(row.pk)
    dispatch.assert_not_called()


def test_the_command_runs_named_or_all_queued_mappings():
    from django.core.management import CommandError, call_command
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[old.pk])
    stored = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')
    with pytest.raises(CommandError):
        call_command('run_queued_repoints')
    call_command('run_queued_repoints', '--all', '--dry-run')
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk
    call_command('run_queued_repoints', str(row.pk))
    stored.refresh_from_db(); row.refresh_from_db()
    assert stored.measurement_concept_id == new.pk and row.deferred_repoint_concept_ids == []


SNOMED_OID = 'urn:oid:2.16.840.1.113883.6.96'


def _code_seen(row, vocabulary, code='6'):
    PatientSourceCode.objects.create(person=row.person, source_value=code,
                                     source_vocabulary_id=vocabulary, omop_table='measurement')


def _temperature_sweep(temperature, vocabulary='urn:oid:hospital-a', code='6'):
    return mapping(temperature, source_code=code, source_vocabulary_id=vocabulary,
                   source_code_description='', deferred_repoint_concept_ids=[0])


def test_the_concept_0_sweep_leaves_rows_another_code_system_sent():
    temperature = ConceptFactory()
    row = mapping(temperature, source_code='6', source_vocabulary_id='urn:oid:hospital-a',
                  source_code_description='', deferred_repoint_concept_ids=[0])
    ours = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    theirs = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    both = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    unknown = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    _code_seen(ours, 'urn:oid:hospital-a')
    _code_seen(theirs, 'urn:oid:hospital-b')
    _code_seen(both, 'urn:oid:hospital-a')
    _code_seen(both, 'urn:oid:hospital-b')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 2

    for stored in (ours, theirs, both, unknown):
        stored.refresh_from_db()
    assert ours.measurement_concept_id == temperature.pk
    assert unknown.measurement_concept_id == temperature.pk
    assert theirs.measurement_concept_id == 0
    assert both.measurement_concept_id == 0


def test_a_code_system_approved_to_the_same_destination_does_not_hold_rows_back():
    temperature = ConceptFactory()
    mapping(temperature, source_code='6', source_vocabulary_id='urn:oid:hospital-b', source_code_description='')
    row = mapping(temperature, source_code='6', source_vocabulary_id='urn:oid:hospital-a',
                  source_code_description='', deferred_repoint_concept_ids=[0])
    both = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    _code_seen(both, 'urn:oid:hospital-a')
    _code_seen(both, 'urn:oid:hospital-b')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 1
    both.refresh_from_db()
    assert both.measurement_concept_id == temperature.pk


def test_another_code_system_does_not_hold_back_a_minted_concept_sweep():
    minted, new = ConceptFactory(), ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[minted.pk])
    stored = MeasurementFactory(measurement_concept=minted, measurement_source_value='GLU')
    _code_seen(stored, 'Other', code='GLU')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 1


def test_a_hospital_scoped_approval_does_not_agree():
    temperature = ConceptFactory()
    mapping(temperature, source_code='6', source_vocabulary_id='urn:oid:hospital-b',
            source_code_description='', organization=OrganizationFactory())
    row = _temperature_sweep(temperature)
    both = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    _code_seen(both, 'urn:oid:hospital-a')
    _code_seen(both, 'urn:oid:hospital-b')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 0


def test_inventory_codes_match_as_rows_do():
    """Case-insensitive, and a longer code sharing the stored prefix blocks."""
    target = ConceptFactory()
    long_code = 'X' * 60
    row = _temperature_sweep(target, vocabulary='Local', code=long_code)
    cased = MeasurementFactory(measurement_concept_id=0, measurement_source_value=long_code[:50].lower())
    prefixed = MeasurementFactory(measurement_concept_id=0, measurement_source_value=long_code[:50])
    _code_seen(cased, 'Other', code=long_code.lower())
    _code_seen(prefixed, 'Local', code=long_code[:55])

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 0


def test_an_oid_alias_agrees_unless_it_holds_a_mapping_of_its_own():
    target = ConceptFactory()
    row = _temperature_sweep(target, vocabulary='SNOMED', code='123')
    aliased = MeasurementFactory(measurement_concept_id=0, measurement_source_value='123')
    _code_seen(aliased, SNOMED_OID, code='123')

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 1

    retained = MeasurementFactory(measurement_concept_id=0, measurement_source_value='123')
    _code_seen(retained, SNOMED_OID, code='123')
    mapping(ConceptFactory(), source_code='123', source_vocabulary_id=SNOMED_OID,
            source_code_description='', status='proposed')
    SourceCodeConceptMapping.objects.filter(pk=row.pk).update(deferred_repoint_concept_ids=[0])

    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 0


# --- a patient-page approval moves that patient's rows in the request (#2791) ---

def _patient_save(client, row, person, **body):
    return client.patch(f'/api/v1/code-mappings/{row.pk}/',
                        {'patient_id': person.person_id, **body}, format='json')


def test_a_patient_page_approval_moves_that_patients_rows_and_nobody_elses(client):
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    mine = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')
    theirs = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    with patch.object(repoint_jobs, 'dispatch_repoint') as job:
        response = _patient_save(client, row, mine.person, status='approved', destination_concept_id=new.pk)
    assert response.status_code == 200, response.data
    # Moved in the request, for this patient: no job.
    job.assert_not_called()
    assert response.data['repoint'] == {
        'queued': False, 'patient_id': mine.person_id, 'from_concept_ids': [old.pk],
        'rows_updated': 1, 'rows_collapsed': 0, 'held': False,
    }
    mine.refresh_from_db(); theirs.refresh_from_db(); row.refresh_from_db()
    assert mine.measurement_concept_id == new.pk
    assert theirs.measurement_concept_id == old.pk
    # The concept stays queued for everyone else: the next run moves them.
    assert row.deferred_repoint_concept_ids == [old.pk]
    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 1
    theirs.refresh_from_db()
    assert theirs.measurement_concept_id == new.pk


def test_a_first_patient_page_approval_claims_that_patients_unresolved_rows(client):
    new = ConceptFactory()
    row = mapping(new, status='proposed')
    mine = MeasurementFactory(measurement_concept_id=0, measurement_source_value='GLU')
    theirs = MeasurementFactory(measurement_concept_id=0, measurement_source_value='GLU')

    response = _patient_save(client, row, mine.person, status='approved')
    assert response.status_code == 200, response.data
    assert (response.data['repoint']['from_concept_ids'], response.data['repoint']['rows_updated']) == ([0], 1)
    mine.refresh_from_db(); theirs.refresh_from_db()
    assert mine.measurement_concept_id == new.pk
    assert theirs.measurement_concept_id == 0


def test_only_the_patients_own_code_systems_can_hold_their_rows_back(client):
    """The other-code-system guard asks about this patient alone: somebody
    else's inventory neither blocks the move nor is read for it."""
    new = ConceptFactory()
    row = mapping(new, status='proposed', source_code='6', source_vocabulary_id='urn:oid:hospital-a',
                  source_code_description='')
    mine = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    theirs = MeasurementFactory(measurement_concept_id=0, measurement_source_value='6')
    _code_seen(mine, 'urn:oid:hospital-a')
    _code_seen(theirs, 'urn:oid:hospital-b')

    response = _patient_save(client, row, mine.person, status='approved')
    assert response.status_code == 200, response.data
    assert response.data['repoint']['rows_updated'] == 1
    mine.refresh_from_db()
    assert mine.measurement_concept_id == new.pk

    # A patient who holds the code under a system with no such approval keeps
    # unresolved rows, as in the job, and the response says none moved.
    response = _patient_save(client, row, theirs.person, status='approved')
    assert response.status_code == 200, response.data
    assert response.data['repoint']['rows_updated'] == 0
    theirs.refresh_from_db()
    assert theirs.measurement_concept_id == 0


def test_a_patient_page_approval_costs_the_same_however_many_patients_hold_the_code(client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    def approve(holders):
        new = ConceptFactory()
        row = mapping(new, status='proposed', source_code=f'N{holders}', source_code_description='')
        rows = [MeasurementFactory(measurement_concept_id=0, measurement_source_value=f'N{holders}')
                for _ in range(holders)]
        for stored in rows:
            _code_seen(stored, 'Local', code=f'N{holders}')
        with CaptureQueriesContext(connection) as queries:
            response = _patient_save(client, row, rows[0].person, status='approved')
        assert response.status_code == 200, response.data
        assert response.data['repoint']['rows_updated'] == 1
        # The guard's own reads of the inventory (it compares the stored,
        # truncated code) name the patient; the response's reads are not its.
        guard = [q['sql'] for q in queries if 'UPPER(SUBSTRING("patient_source_code"."source_value"' in q['sql']]
        assert guard and all('"patient_source_code"."person_id" = ' in sql for sql in guard), guard
        return len(queries)

    assert approve(1) == approve(6)


def test_a_patient_page_save_that_only_proposes_moves_nothing(client):
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old, status='proposed')
    mine = MeasurementFactory(measurement_concept_id=0, measurement_source_value='GLU')

    response = _patient_save(client, row, mine.person, status='proposed', destination_concept_id=new.pk)
    assert response.status_code == 200, response.data
    assert response.data['repoint'] is None
    mine.refresh_from_db()
    assert mine.measurement_concept_id == 0


def test_a_hospital_mapping_approved_from_a_patient_page_moves_only_that_hospitals_rows(client):
    from django.contrib.contenttypes.models import ContentType

    from omop_core.models import Measurement, ProvenanceRecord
    hospital, new = OrganizationFactory(), ConceptFactory()
    row = mapping(new, status='proposed', organization=hospital,
                  source_vocabulary_id='https://fhir.cerner.com/993df7f9/codeSet/72')
    from_hospital = MeasurementFactory(measurement_concept_id=0, measurement_source_value='GLU')
    unattributed = MeasurementFactory(
        person=from_hospital.person, measurement_concept_id=0, measurement_source_value='GLU',
        measurement_date='2024-02-01')
    ProvenanceRecord.objects.create(
        source='etl', organization=hospital, target_patient_id=str(from_hospital.person_id),
        content_type=ContentType.objects.get_for_model(Measurement), object_id=from_hospital.pk)

    response = _patient_save(client, row, from_hospital.person, status='approved')
    assert response.status_code == 200, response.data
    assert response.data['repoint']['rows_updated'] == 1
    from_hospital.refresh_from_db(); unattributed.refresh_from_db()
    assert from_hospital.measurement_concept_id == new.pk
    assert unattributed.measurement_concept_id == 0


@pytest.fixture
def worker(settings):
    """A deployment with a broker, and nothing else that would use it."""
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'
    settings.PROVISIONAL_DATA_ENABLED = False


def test_with_a_worker_the_patients_record_is_re_derived_after_the_save_commits(
        client, worker, django_capture_on_commit_callbacks):
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    mine = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')
    MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    with use_dispatcher(FakeDispatcher()) as derivations:
        with django_capture_on_commit_callbacks() as callbacks:
            response = _patient_save(client, row, mine.person, status='approved', destination_concept_id=new.pk)
            assert response.status_code == 200, response.data
            # Not inside the save: a worker would derive from the rows as they were.
            assert derivations.calls == []
        for callback in callbacks:
            callback()
    # This patient only; the other waits for Generate OMOP, which derives theirs.
    assert derivations.calls == [mine.person_id]


def test_without_a_worker_nothing_is_derived_in_the_request(client, django_capture_on_commit_callbacks):
    """12-32s on a bulk-loaded patient: the record is marked stale and left."""
    from omop_core.models import PatientRecord
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    mine = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')
    # The signal chain derived a record when the measurement landed.
    record, _ = PatientRecord.objects.get_or_create(person=mine.person)
    PatientRecord.objects.filter(pk=record.pk).update(derivation_version=7)

    with use_dispatcher(FakeDispatcher()) as derivations, django_capture_on_commit_callbacks(execute=True):
        response = _patient_save(client, row, mine.person, status='approved', destination_concept_id=new.pk)
    assert response.status_code == 200, response.data
    assert response.data['repoint']['rows_updated'] == 1
    assert derivations.calls == []
    record.refresh_from_db()
    assert record.derivation_version == 0


def test_no_derivation_when_no_row_of_the_patient_moved(client, worker, django_capture_on_commit_callbacks):
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    theirs = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')
    bystander = MeasurementFactory(measurement_concept=old, measurement_source_value='OTHER')

    with use_dispatcher(FakeDispatcher()) as derivations, django_capture_on_commit_callbacks(execute=True):
        response = _patient_save(client, row, bystander.person, status='approved', destination_concept_id=new.pk)
    assert response.status_code == 200, response.data
    assert response.data['repoint']['rows_updated'] == 0
    assert derivations.calls == []
    theirs.refresh_from_db()
    assert theirs.measurement_concept_id == old.pk


@pytest.mark.django_db(transaction=True)
def test_a_derivation_that_cannot_be_queued_does_not_fail_the_patient_page_save(client, worker):
    """A real commit, so the hook runs as it does in a request: the worker's
    dispatcher defers to commit itself, and that has already happened."""
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    mine = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')

    with patch('omop_core.tasks.refresh_patient_record_task.apply_async',
               side_effect=ConnectionError('no broker')) as queued:
        response = _patient_save(client, row, mine.person, status='approved', destination_concept_id=new.pk)
    assert response.status_code == 200, response.data
    queued.assert_called_once()
    assert queued.call_args.kwargs['args'] == [mine.person_id]
    mine.refresh_from_db()
    assert mine.measurement_concept_id == new.pk


@pytest.mark.django_db(transaction=True)
def test_rows_another_transaction_holds_are_left_queued_and_the_approval_is_saved(client, monkeypatch):
    """An all-patients sweep keeps a patient's rows locked until it commits. The
    save does not wait for it: the mapping is approved, the rows stay queued."""
    import threading

    from django.db import connections, transaction

    from omop_core.models import Measurement
    monkeypatch.setattr(repoint_jobs, 'PERSON_MOVE_LOCK_TIMEOUT_MS', 200)
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    mine = MeasurementFactory(measurement_concept=old, measurement_source_value='GLU')
    locked, release = threading.Event(), threading.Event()

    def hold_the_row():
        try:
            with transaction.atomic():
                Measurement.objects.select_for_update().get(pk=mine.pk)
                locked.set()
                release.wait(20)
        finally:
            connections.close_all()

    holder = threading.Thread(target=hold_the_row)
    holder.start()
    try:
        assert locked.wait(10)
        response = _patient_save(client, row, mine.person, status='approved', destination_concept_id=new.pk)
    finally:
        release.set()
        holder.join()

    assert response.status_code == 200, response.data
    assert response.data['repoint'] == {
        'queued': False, 'patient_id': mine.person_id, 'from_concept_ids': [old.pk],
        'rows_updated': 0, 'rows_collapsed': 0, 'held': True,
    }
    row.refresh_from_db(); mine.refresh_from_db()
    assert (row.status, row.target_concept_id, row.deferred_repoint_concept_ids) == ('approved', new.pk, [old.pk])
    assert mine.measurement_concept_id == old.pk
    # Still queued, so the next run moves them.
    assert repoint_jobs.run_repoint(row.pk)['rows_updated'] == 1


def test_any_patient_page_save_of_an_approved_mapping_applies_it_to_that_patient(client):
    """The mapping was approved from another patient's page, so concept 0 is
    still waiting for everyone else. A save that changes only the note, from
    this patient's page, moves this patient's rows: the mapping is approved
    and their rows were not on it, which is what Generate OMOP would do."""
    new = ConceptFactory()
    row = mapping(new, deferred_repoint_concept_ids=[0])
    mine = MeasurementFactory(measurement_concept_id=0, measurement_source_value='GLU')
    theirs = MeasurementFactory(measurement_concept_id=0, measurement_source_value='GLU')

    response = _patient_save(client, row, mine.person, notes='checked against the report')
    assert response.status_code == 200, response.data
    assert response.data['repoint']['rows_updated'] == 1
    mine.refresh_from_db(); theirs.refresh_from_db(); row.refresh_from_db()
    assert mine.measurement_concept_id == new.pk
    assert theirs.measurement_concept_id == 0
    assert row.deferred_repoint_concept_ids == [0]


def test_a_patient_the_approver_cannot_write_is_refused_and_no_row_moves(django_user_model):
    """The scope is checked before anything is written, and a refused scope
    never falls back to every patient (#1814)."""
    from omop_core.models import GroupAccess
    from tests.factories import PatientRecordFactory
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    elsewhere = PatientRecordFactory()
    stored = MeasurementFactory(person=elsewhere.person, measurement_concept=old, measurement_source_value='GLU')
    admin = django_user_model.objects.create_user(email='other-hospital-admin@test.com')
    GroupAccess.objects.create(identity=admin, org=OrganizationFactory(), role='org_admin')
    api = APIClient()
    api.force_authenticate(admin)

    response = _patient_save(api, row, elsewhere.person, status='approved', destination_concept_id=new.pk)
    assert response.status_code == 403, response.data
    stored.refresh_from_db(); row.refresh_from_db()
    assert stored.measurement_concept_id == old.pk
    assert (row.target_concept_id, row.deferred_repoint_concept_ids) == (old.pk, [])

    missing = _patient_save(api, row, type('P', (), {'person_id': 987654321}), status='approved',
                            destination_concept_id=new.pk)
    assert missing.status_code == 400, missing.data
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk
