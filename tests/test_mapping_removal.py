"""Delete in Edit Mapping: a code patient data carries keeps its queue row (#2744)."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    GroupAccess, PatientRecord, PatientSourceCode, SourceCodeConceptMapping,
)
from omop_core.services import mapping_removal, repoint_jobs
from omop_core.test_utils import ensure_test_concept_zero
from patient_portal.models import Identity
from tests.factories import ConceptFactory, MeasurementFactory, OrganizationFactory, PersonFactory

pytestmark = pytest.mark.django_db

CERNER = 'https://fhir.cerner.com/993df7f9/codeSet/72'


@pytest.fixture
def staff():
    return Identity.objects.create_user(email='removal@example.test', is_staff=True)


@pytest.fixture
def client(staff):
    instance = APIClient()
    instance.force_authenticate(staff)
    return instance


def mapping(target, **overrides):
    return SourceCodeConceptMapping.objects.create(**{
        'source_code': '700540689', 'source_vocabulary_id': CERNER,
        'source_code_description': 'serum e/p gamma m-spike 1',
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'target_concept': target,
        'destination_vocabulary_id': 'LOINC', 'status': 'approved', 'origin': 'import',
        'occurrence_count': 718, 'notes': 'check the band', **overrides,
    })


def url(row):
    return f'/api/v1/code-mappings/{row.pk}/'


def stored_result(concept, source_value='700540689'):
    ensure_test_concept_zero()
    person = PersonFactory()
    PatientRecord.objects.create(person=person, derivation_version=1)
    return MeasurementFactory(person=person, measurement_concept=concept, measurement_source_value=source_value)


def test_a_code_in_patient_data_keeps_its_row_and_loses_only_the_destination(client, staff, django_capture_on_commit_callbacks):
    old = ConceptFactory()
    row = mapping(old, reviewer=staff, proposed_by=staff, origin_system='A Curator', suggestion_confidence=0.9,
                  destination_unit_concept=ConceptFactory(), locked_by=staff)
    stored = stored_result(old)

    with patch.object(repoint_jobs, 'repoint_clinical_rows', wraps=repoint_jobs.repoint_clinical_rows) as moved, \
            django_capture_on_commit_callbacks() as callbacks:
        response = client.delete(url(row))
        # The request itself moves nothing: that was the freeze.
        moved.assert_not_called()

    assert response.status_code == 200, response.data
    assert response.data['removed'] is False
    assert response.data['repoint'] == {'queued': True, 'from_concept_ids': [old.pk], 'to_concept_id': 0}
    assert (response.data['mapping_id'], response.data['status'], response.data['destination_concept_id']) == (
        row.pk, 'proposed', None)
    row.refresh_from_db()
    assert (row.target_concept_id, row.status, row.destination_vocabulary_id) == (None, 'proposed', '')
    assert (row.reviewer_id, row.reviewed_at, row.proposed_by_id, row.locked_by_id) == (None, None, None, None)
    assert (row.destination_unit_concept_id, row.suggestion_confidence, row.origin_system) == (None, None, '')
    # The source side and the curator's own words are untouched.
    assert (row.source_code, row.source_code_description, row.occurrence_count, row.notes, row.origin) == (
        '700540689', 'serum e/p gamma m-spike 1', 718, 'check the band', 'import')
    assert row.updated_by_id == staff.pk
    assert row.pending_repoint_concept_ids == [old.pk]
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk

    for callback in callbacks:
        callback()
    stored.refresh_from_db(); row.refresh_from_db()
    assert stored.measurement_concept_id == 0
    assert PatientRecord.objects.get(person=stored.person).derivation_version == 0
    assert row.pending_repoint_concept_ids == []


def test_pressing_delete_again_changes_nothing_more(client, django_capture_on_commit_callbacks):
    row = mapping(ConceptFactory())
    with django_capture_on_commit_callbacks(execute=True):
        assert client.delete(url(row)).status_code == 200

    again = client.delete(url(row))

    assert again.status_code == 400 and again.data['detail'] == 'Cannot delete an unmapped source code.'
    assert SourceCodeConceptMapping.objects.filter(pk=row.pk, target_concept__isnull=True).exists()


@pytest.mark.parametrize('inventory_vocabulary', [CERNER, 'urn:oid:2.16.840.1.113883.6.96'])
def test_the_inventory_counts_as_patient_data_when_the_seen_count_is_zero(client, inventory_vocabulary):
    vocabulary = 'SNOMED' if inventory_vocabulary.startswith('urn:oid') else CERNER
    row = mapping(ConceptFactory(), occurrence_count=0, source_vocabulary_id=vocabulary)
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='700540689',
                                     source_vocabulary_id=inventory_vocabulary, omop_table='measurement')
    # The same code in another code system is another code.
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='700540689',
                                     source_vocabulary_id='EPIC', omop_table='measurement')

    assert mapping_removal.delete_effect(row) == 'unmap'
    assert client.delete(url(row)).status_code == 200
    assert SourceCodeConceptMapping.objects.filter(pk=row.pk).exists()


def test_a_mapping_for_a_code_no_patient_data_carries_is_removed(client):
    old = ConceptFactory()
    row = mapping(old, occurrence_count=0)
    PatientSourceCode.objects.create(person=PersonFactory(), source_value='700540689',
                                     source_vocabulary_id='EPIC', omop_table='measurement')
    stray = stored_result(old)

    response = client.delete(url(row))

    assert response.status_code == 204
    assert not SourceCodeConceptMapping.objects.filter(pk=row.pk).exists()
    # Whatever rows it did point are un-mapped before it goes, as before.
    stray.refresh_from_db()
    assert stray.measurement_concept_id == 0


def test_the_dialog_is_told_what_delete_will_do(client, settings):
    seen, unseen, unmapped = (mapping(ConceptFactory()), mapping(ConceptFactory(), source_code='TYPO', occurrence_count=0),
                              mapping(None, source_code='QUEUED', status='proposed'))

    assert [client.get(url(row)).data['delete_effect'] for row in (seen, unseen, unmapped)] == ['unmap', 'delete', None]

    secret = 'removal-service-secret-00000000000000000'
    settings.SERVICE_AUTH_TOKENS = {'dbt': {'token': secret, 'scopes': 'system/*.read'}}
    settings.SERVICE_AUTH_TOKEN = ''
    machine = APIClient()
    machine.credentials(HTTP_AUTHORIZATION=f'Bearer {secret}')
    assert machine.get(url(seen)).data['delete_effect'] is None


def test_the_next_approval_moves_rows_a_lost_background_move_left_behind(client, django_capture_on_commit_callbacks):
    old, new = ConceptFactory(), ConceptFactory()
    row = mapping(old)
    stored = stored_result(old)
    with django_capture_on_commit_callbacks():      # captured, never run: the task was lost
        assert client.delete(url(row)).status_code == 200
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk

    with django_capture_on_commit_callbacks(execute=True):
        response = client.patch(url(row), {'status': 'approved', 'destination_concept_id': new.pk}, format='json')

    assert response.status_code == 200, response.data
    stored.refresh_from_db()
    assert stored.measurement_concept_id == new.pk


def test_the_background_move_leaves_rows_alone_when_the_destination_is_back():
    old = ConceptFactory()
    row = mapping(old)
    stored = stored_result(old)

    assert repoint_jobs.run_unpoint(row.pk, old.pk)['rows_updated'] == 0
    assert repoint_jobs.run_unpoint(999_999_999, old.pk)['rows_updated'] == 0

    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk


def test_the_background_move_waits_for_a_re_point_of_the_same_mapping(settings):
    from omop_core import tasks

    old = ConceptFactory()
    row = mapping(None, status='proposed', pending_repoint_concept_ids=[old.pk])
    stored = stored_result(old)

    with patch.object(repoint_jobs, '_advisory_lock', return_value=False):
        assert repoint_jobs.run_unpoint(row.pk, old.pk)['busy'] is True
        with pytest.raises(tasks._MappingBusy):
            tasks.unpoint_mapping_rows_task.run(row.pk, old.pk)
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk

    assert tasks.unpoint_mapping_rows_task.run(row.pk, old.pk) == {
        'mapping_id': row.pk, 'old_concept_id': old.pk, 'rows_updated': 1, 'persons_marked_stale': 1, 'rows_collapsed': 0}
    stored.refresh_from_db()
    assert stored.measurement_concept_id == 0


def test_a_broker_queues_the_move_and_a_queue_outage_runs_it_inline(settings):
    settings.CELERY_BROKER_URL = 'redis://example.invalid/0'
    old = ConceptFactory()
    row = mapping(None, status='proposed', pending_repoint_concept_ids=[old.pk])
    stored = stored_result(old)

    with patch('omop_core.tasks.unpoint_mapping_rows_task.delay') as delay:
        repoint_jobs.dispatch_unpoint(row.pk, old.pk)
    delay.assert_called_once_with(row.pk, old.pk)
    stored.refresh_from_db()
    assert stored.measurement_concept_id == old.pk

    with patch('omop_core.tasks.unpoint_mapping_rows_task.delay', side_effect=ConnectionError('no broker')):
        repoint_jobs.dispatch_unpoint(row.pk, old.pk)
    stored.refresh_from_db()
    assert stored.measurement_concept_id == 0


def test_the_guards_on_delete_are_unchanged(staff):
    row = mapping(ConceptFactory())
    doctor = Identity.objects.create_user(email='doctor-removal@example.test')
    analyst = Identity.objects.create_user(email='analyst-removal@example.test')
    organization = OrganizationFactory()
    GroupAccess.objects.create(identity=doctor, org=organization, role='doctor')
    GroupAccess.objects.create(identity=analyst, org=organization, role='analyst')

    def delete_as(user):
        client = APIClient()
        client.force_authenticate(user)
        return client.delete(url(row)).status_code

    # A reader writes nothing; an analyst may not undo an approval.
    assert (delete_as(doctor), delete_as(analyst)) == (403, 403)
    SourceCodeConceptMapping.objects.filter(pk=row.pk).update(locked_by=staff, locked_at='2099-01-01T00:00:00Z')
    other = Identity.objects.create_user(email='other-staff@example.test', is_staff=True)
    assert delete_as(other) == 423
    assert SourceCodeConceptMapping.objects.filter(pk=row.pk, target_concept__isnull=False).exists()
