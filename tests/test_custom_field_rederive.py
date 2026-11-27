"""A created field is filled from the facts patients already have at its concept (#2941).

Creating, restoring or re-mapping a created field queues the derivation of the records of
the people with a live fact at the mapping's concept, off the request. Nothing here runs
Celery: ``dispatch`` is replaced, and the chunk it would run is called directly.
"""
from decimal import Decimal
from io import StringIO

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from omop_core import tasks
from omop_core.models import CustomPatientField, FieldConceptMapping, PatientRecord
from omop_core.services import custom_field_rederive, custom_field_removal, custom_field_values
from omop_core.services.omop_projection import PATIENT_SELF_REPORT_TYPE
from patient_portal.models import Identity
from tests.factories import (
    ConceptFactory, DomainFactory, MeasurementFactory, PatientRecordFactory, VocabularyFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('disease_extensions')]

EHR = 32817
CUSTOM = '/api/v1/custom-patient-fields/'
NAME = 'psa_level'


@pytest.fixture(autouse=True)
def broker(settings):
    settings.CELERY_BROKER_URL = 'redis://broker.invalid:6379/0'
    cache.clear()
    custom_field_rederive._warned.clear()
    ConceptFactory(concept_id=EHR, concept_code='ehr-type')
    ConceptFactory(concept_id=PATIENT_SELF_REPORT_TYPE, concept_code='patient-report-type')
    yield
    cache.clear()


@pytest.fixture
def queued(monkeypatch):
    calls = []
    monkeypatch.setattr(custom_field_rederive, 'dispatch',
                        lambda mapping_id, after=0: calls.append((mapping_id, after)))
    return calls


@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='curator@example.org', is_staff=True))
    return client


@pytest.fixture
def psa():
    return ConceptFactory(concept_code='2857-1', concept_name='PSA [Mass/volume] in Serum or Plasma',
                          vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
                          domain=DomainFactory(domain_id='Measurement', domain_name='Measurement'))


def make(client, concept, expect=201, **changes):
    body = {
        'confirm_patient_record': True, 'field_name': NAME, 'display_name': 'PSA level',
        'field_type': 'number', 'tab': 'labs', 'concept': concept.concept_id,
        'omop_table': 'measurement', 'diseases': [], 'answers': [],
    }
    body.update(changes)
    response = client.post(CUSTOM, body, format='json')
    assert response.status_code == expect, response.data
    return response


def patient_with_psa(psa, value='27', **fact):
    record = PatientRecordFactory()
    MeasurementFactory(person=record.person, measurement_concept=psa,
                       measurement_source_value='PSA', value_as_number=Decimal(value), **fact)
    # A fact written after the field exists derives its record by signal; the case under
    # test is a record that was derived before the field did, so it is left stale.
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={})
    return record


def stored(record):
    return PatientRecord.objects.get(pk=record.pk).custom_fields or {}


# ----------------------------------------------------------------- what queues it


def test_creating_a_field_queues_the_derivation(staff, psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    mapping = CustomPatientField.objects.get(field_name=NAME).mapping
    assert queued == [(mapping.pk, 0)]


def test_a_record_already_holding_the_concepts_facts_is_filled_by_the_chunk(
        staff, psa, queued, django_capture_on_commit_callbacks):
    record = patient_with_psa(psa)
    assert stored(record) == {}
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    assert stored(record) == {}   # nothing derives in the request

    result = custom_field_rederive.run_chunk(queued[0][0])

    assert result['refreshed'] == 1 and result['next_after'] is None
    assert stored(record) == {NAME: 27}


def test_restoring_a_field_queues_the_derivation(staff, psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    queued.clear()
    cache.clear()
    assert staff.delete(f'{CUSTOM}{NAME}/').status_code == 200
    assert queued == []   # retiring changes no data and queues nothing

    with django_capture_on_commit_callbacks(execute=True):
        assert staff.post(f'{CUSTOM}{NAME}/restore/').status_code == 200

    assert len(queued) == 1


def test_moving_the_mapping_to_another_concept_queues_it_and_a_note_edit_does_not(
        staff, psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    queued.clear()
    cache.clear()
    mapping = CustomPatientField.objects.get(field_name=NAME).mapping

    with django_capture_on_commit_callbacks(execute=True):
        mapping.notes = 'a note'
        mapping.save()
    assert queued == []

    other = ConceptFactory(concept_code='9999-9', vocabulary=psa.vocabulary, domain=psa.domain)
    with django_capture_on_commit_callbacks(execute=True):
        mapping = FieldConceptMapping.objects.get(pk=mapping.pk)
        mapping.concept = other
        mapping.concept_code = other.concept_code
        mapping.save()
    assert queued == [(mapping.pk, 0)]


def test_approving_a_proposed_created_field_mapping_queues_it(
        staff, psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    mapping = CustomPatientField.objects.get(field_name=NAME).mapping
    FieldConceptMapping.objects.filter(pk=mapping.pk).update(status='proposed')
    queued.clear()
    cache.clear()

    with django_capture_on_commit_callbacks(execute=True):
        mapping = FieldConceptMapping.objects.get(pk=mapping.pk)
        mapping.status = 'approved'
        mapping.save()

    assert queued == [(mapping.pk, 0)]


def test_a_built_in_fields_mapping_queues_nothing(psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        FieldConceptMapping.objects.create(
            field_name='ecog_performance_status', concept=psa, concept_code=psa.concept_code,
            omop_table='measurement', status='approved')
    assert queued == []


def test_a_burst_of_saves_queues_one_chain(staff, psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
        mapping = CustomPatientField.objects.get(field_name=NAME).mapping
        custom_field_rederive.queue(mapping.pk)
        custom_field_rederive.queue(mapping.pk)
    assert len(queued) == 1


def test_with_no_broker_nothing_is_queued(settings, staff, psa, queued, django_capture_on_commit_callbacks):
    settings.CELERY_BROKER_URL = ''
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    assert queued == []


def test_a_failure_to_queue_never_fails_the_save(staff, psa, monkeypatch, django_capture_on_commit_callbacks):
    def boom(mapping_id, after=0):
        raise RuntimeError('broker down')
    monkeypatch.setattr(custom_field_rederive, 'dispatch', boom)
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    assert CustomPatientField.objects.filter(field_name=NAME).exists()


# ------------------------------------------------------------------ the work


@pytest.fixture
def field(staff, psa, queued, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        make(staff, psa)
    queued.clear()
    return CustomPatientField.objects.select_related('mapping').get(field_name=NAME)


def test_only_people_with_a_live_fact_at_the_concept_are_selected(psa, field):
    one = patient_with_psa(psa)
    two = patient_with_psa(psa)
    patient_with_psa(psa, value='9', is_erroneous=True)
    other_concept = ConceptFactory(concept_code='1-1', vocabulary=psa.vocabulary, domain=psa.domain)
    elsewhere = PatientRecordFactory()
    MeasurementFactory(person=elsewhere.person, measurement_concept=other_concept,
                       value_as_number=Decimal('3'))
    twice = patient_with_psa(psa)
    MeasurementFactory(person=twice.person, measurement_concept=psa, value_as_number=Decimal('30'),
                       measurement_date='2024-03-01')

    ids = custom_field_rederive.person_ids(field.mapping)

    assert ids == sorted({one.person_id, two.person_id, twice.person_id})


def test_chunks_hand_on_a_cursor_until_everyone_is_derived(psa, field):
    records = [patient_with_psa(psa, value=str(n + 1)) for n in range(5)]
    first = custom_field_rederive.run_chunk(field.mapping_id, 0, limit=2)
    second = custom_field_rederive.run_chunk(field.mapping_id, first['next_after'], limit=2)
    third = custom_field_rederive.run_chunk(field.mapping_id, second['next_after'], limit=2)

    assert (first['refreshed'], second['refreshed'], third['refreshed']) == (2, 2, 1)
    assert third['next_after'] is None
    assert sorted(stored(r).get(NAME) for r in records) == [1, 2, 3, 4, 5]


def test_a_chunk_that_runs_out_of_budget_hands_on_the_last_person_it_finished(psa, field):
    records = [patient_with_psa(psa) for _ in range(3)]
    result = custom_field_rederive.run_chunk(field.mapping_id, 0, limit=3, budget=-1)
    assert result['refreshed'] == 0 and result['next_after'] == 0
    assert all(stored(r) == {} for r in records)


def test_one_record_failing_does_not_stop_the_rest(psa, field, monkeypatch):
    from omop_core.services import patient_record_service

    bad, good = patient_with_psa(psa), patient_with_psa(psa)
    real = patient_record_service.refresh_patient_record

    def refresh(person):
        if person.person_id == bad.person_id:
            raise RuntimeError('boom')
        return real(person)
    monkeypatch.setattr(patient_record_service, 'refresh_patient_record', refresh)

    result = custom_field_rederive.run_chunk(field.mapping_id, 0)

    assert result['errors'] == 1 and result['refreshed'] == 1
    assert stored(good) == {NAME: 27}


def test_a_chunk_for_a_retired_field_derives_nothing(staff, psa, field):
    patient_with_psa(psa)
    custom_field_removal.retire(field)
    assert custom_field_rederive.run_chunk(field.mapping_id)['refreshed'] == 0


def test_the_first_chunk_releases_the_coalescing_mark(field):
    cache.add(custom_field_rederive._pending_key(field.mapping_id), 1, 300)
    custom_field_rederive.run_chunk(field.mapping_id, 0)
    assert cache.add(custom_field_rederive._pending_key(field.mapping_id), 1, 300)


def test_the_task_queues_the_next_chunk_with_its_cursor(psa, field, monkeypatch):
    for _ in range(custom_field_rederive.CHUNK + 1):
        patient_with_psa(psa)
    chained = []
    monkeypatch.setattr(tasks.refresh_custom_field_records_task, 'delay',
                        lambda *args: chained.append(args))

    result = tasks.refresh_custom_field_records_task(field.mapping_id, 0)

    assert result['refreshed'] == custom_field_rederive.CHUNK
    assert chained == [(field.mapping_id, result['next_after'])]


def test_the_command_derives_everyone_in_the_foreground(psa, field):
    records = [patient_with_psa(psa) for _ in range(3)]
    out = StringIO()
    call_command('refresh_custom_field_records', NAME, stdout=out)
    assert '3 records derived' in out.getvalue()
    assert all(stored(r) == {NAME: 27} for r in records)


def test_the_command_dry_run_derives_nobody(psa, field):
    record = patient_with_psa(psa)
    out = StringIO()
    call_command('refresh_custom_field_records', NAME, '--dry-run', stdout=out)
    assert '1 records would be derived' in out.getvalue()
    assert stored(record) == {}


def test_the_command_refuses_an_unknown_field():
    with pytest.raises(CommandError):
        call_command('refresh_custom_field_records', 'no_such_field')


# --------------------------------------------------- a text field over a number


class _Row:
    def __init__(self, number=None, text=None, name=None):
        self.value_as_number = number
        self.value_as_string = text
        self.value_as_concept = type('C', (), {'concept_name': name})() if name else None


@pytest.mark.parametrize('number, expected', [
    (Decimal('27'), '27'), (Decimal('0.50000'), '0.5'), (Decimal('4.12345'), '4.12345'),
])
def test_a_text_field_reads_a_numeric_result_as_its_number(number, expected):
    row = _Row(number=number, name='No matching concept')
    assert custom_field_values.read_value('text', row, entered=False) == expected


def test_a_text_field_still_prefers_the_rows_text_and_then_the_concept_name():
    assert custom_field_values.read_value('text', _Row(number=Decimal('1'), text='<0.1'),
                                          entered=False) == '<0.1'
    assert custom_field_values.read_value('text', _Row(name='Positive'), entered=False) == 'Positive'


# ----------------------------------------------------- deleting a mirroring field


def test_a_field_that_only_mirrors_labs_can_be_deleted_and_its_values_leave_the_records(
        staff, psa, field):
    record = patient_with_psa(psa)
    custom_field_rederive.run_chunk(field.mapping_id)
    assert stored(record) == {NAME: 27}

    data = staff.get(f'{CUSTOM}{NAME}/').data
    assert data['records_with_value'] == 1 and data['records_with_entered_value'] == 0
    assert data['can_delete'] is True

    response = staff.delete(f'{CUSTOM}{NAME}/?permanent=true')

    assert response.status_code == 200
    assert not CustomPatientField.objects.filter(field_name=NAME).exists()
    assert NAME not in stored(record)
    assert record.person.measurement_set.filter(measurement_concept=psa).exists()


def test_an_entry_nobody_has_represented_in_omop_still_blocks_a_delete(staff, psa, field):
    record = PatientRecordFactory()
    PatientRecord.objects.filter(pk=record.pk).update(
        custom_fields={NAME: 5}, user_edited_fields=[custom_field_values.pending_key(NAME)])

    data = staff.get(f'{CUSTOM}{NAME}/').data

    assert data['records_with_entered_value'] == 1 and data['can_delete'] is False
    assert staff.delete(f'{CUSTOM}{NAME}/?permanent=true').status_code == 409
    assert stored(record) == {NAME: 5}
