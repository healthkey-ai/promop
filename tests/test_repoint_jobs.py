"""A mapping save queues the stored-row re-point; a job moves the rows."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from omop_core.services import repoint_jobs
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
