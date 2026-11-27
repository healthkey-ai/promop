"""Removing a created field: retire by default, delete only when nothing depends on it.

``DELETE /api/v1/custom-patient-fields/<name>/`` retires a created field (hidden
everywhere, every stored value kept, restorable); ``?permanent=true`` deletes it for
good and only when no record holds a value and no OMOP fact is written under it.
``DELETE /api/v1/field-mappings/<id>/`` on a created field's mapping used to be a 500
(``CustomPatientField.mapping`` is PROTECT); it is now a 409 naming the field's own action.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    CustomPatientField, FieldChoice, FieldConceptMapping, FieldDiseaseMembership, FieldFormula,
    GroupAccess, Observation, PatientRecord, ProposalRejection,
)
from omop_core.services import custom_field_removal
from omop_core.services.omop_projection import PATIENT_SELF_REPORT_TYPE
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory, DomainFactory, ObservationFactory, OrganizationFactory, PatientRecordFactory,
    VocabularyFactory,
)

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('disease_extensions')]

EHR = 32817
CUSTOM = '/api/v1/custom-patient-fields/'
WRITABLE = '/api/v1/patient-records/writable-fields/'
FIELD_MAPPINGS = '/api/v1/field-mappings/'
NAME = 'gleason_grade_group'


def url(name=NAME, suffix=''):
    return f'{CUSTOM}{name}/{suffix}'


@pytest.fixture(autouse=True)
def types(settings):
    settings.CELERY_BROKER_URL = ''
    ConceptFactory(concept_id=EHR, concept_code='ehr-type')
    ConceptFactory(concept_id=PATIENT_SELF_REPORT_TYPE, concept_code='patient-report-type')


@pytest.fixture
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='curator@example.org', is_staff=True))
    return client


def as_role(role):
    user = Identity.objects.create_user(email=f'{role}@example.org')
    GroupAccess.objects.create(identity=user, org=OrganizationFactory(), role=role)
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def concepts():
    observation = DomainFactory(domain_id='Observation', domain_name='Observation')
    snomed = VocabularyFactory(vocabulary_id='SNOMED')
    return {
        'question': ConceptFactory(concept_code='Q-GRADE', concept_name='Gleason grade group',
                                   vocabulary=snomed, domain=observation),
        'one': ConceptFactory(concept_code='A-1', concept_name='Grade group 1',
                              vocabulary=snomed, domain=observation),
    }


def make(client, concepts, expect=201, **changes):
    body = {
        'confirm_patient_record': True, 'field_name': NAME, 'display_name': 'Gleason grade group',
        'field_type': 'choice', 'tab': 'disease', 'concept': concepts['question'].concept_id,
        'omop_table': 'observation', 'diseases': ['MM'],
        'answers': [{'label': 'Grade group 1', 'value': '1', 'concept': concepts['one'].concept_id},
                    {'label': 'Grade group 2', 'value': '2'}],
    }
    body.update(changes)
    response = client.post(CUSTOM, body, format='json')
    assert response.status_code == expect, response.data
    return response


@pytest.fixture
def grade(staff, concepts):
    make(staff, concepts)
    return CustomPatientField.objects.get(field_name=NAME)


@pytest.fixture
def record():
    return PatientRecordFactory()


def patch(client, record, values, expect=200):
    response = client.patch(f'/api/patient-info/{record.person_id}/', {'custom_fields': values}, format='json')
    assert response.status_code == expect, response.data
    return response


def stored(record):
    return PatientRecord.objects.get(pk=record.pk).custom_fields or {}


def listed(client):
    return [f['field_name'] for f in client.get(CUSTOM).data]


def descriptors(client):
    return {d['field_name']: d for d in client.get(FIELD_MAPPINGS).data}


# ------------------------------------------------------- the 500 this replaces


def test_deleting_a_created_fields_mapping_is_refused_and_names_the_field_action(staff, grade):
    response = staff.delete(f'{FIELD_MAPPINGS}{grade.mapping_id}/')
    assert response.status_code == 409
    assert response.data['code'] == 'created_field' and response.data['field_name'] == NAME
    assert f'/api/v1/custom-patient-fields/{NAME}/' in response.data['detail']
    assert FieldConceptMapping.objects.filter(pk=grade.mapping_id).exists()
    assert CustomPatientField.objects.filter(pk=grade.pk).exists()


def test_a_built_in_fields_mapping_still_deletes(staff):
    mapping = FieldConceptMapping.objects.create(field_name='ecog_performance_status', status='proposed')
    assert staff.delete(f'{FIELD_MAPPINGS}{mapping.pk}/').status_code == 204


def test_a_retired_fields_mapping_cannot_be_changed(staff, grade):
    assert staff.delete(url()).status_code == 200
    response = staff.patch(f'{FIELD_MAPPINGS}{grade.mapping_id}/', {'status': 'approved'}, format='json')
    assert response.status_code == 409 and response.data['code'] == 'retired_field'
    assert FieldConceptMapping.objects.get(pk=grade.mapping_id).status == 'rejected'


# ------------------------------------------------------------------ who may


@pytest.mark.parametrize('role', ['analyst', 'doctor', 'patient'])
def test_only_staff_and_org_admins_may_remove_or_restore_a_field(staff, grade, role):
    client = as_role(role)
    assert client.delete(url()).status_code == 403
    assert client.delete(url() + '?permanent=true').status_code == 403
    assert client.get(url()).status_code == 403
    assert client.post(url(suffix='restore/')).status_code == 403
    assert client.get(CUSTOM + '?state=retired').status_code == 403
    grade.refresh_from_db()
    assert grade.retired_at is None and grade.mapping.status == 'approved'
    # A refusal does not depend on the field existing.
    assert client.delete(url('no_such_field')).status_code == 403


def test_an_unauthenticated_caller_is_refused(grade):
    assert APIClient().delete(url()).status_code in (401, 403)


def test_an_org_admin_can_remove_and_restore(concepts):
    client = as_role('org_admin')
    make(client, concepts)
    assert client.delete(url()).status_code == 200
    assert client.post(url(suffix='restore/')).status_code == 200
    assert NAME in listed(client)


def test_an_unknown_field_is_a_404(staff):
    assert staff.delete(url('no_such_field')).status_code == 404
    assert staff.post(url('no_such_field', 'restore/')).status_code == 404
    assert staff.get(url('no_such_field')).status_code == 404


# ------------------------------------------------------------------- retiring


def test_retiring_hides_the_field_everywhere_a_reader_looks(staff, grade, record):
    assert NAME in listed(staff) and NAME in descriptors(staff) and NAME in staff.get(WRITABLE).data

    response = staff.delete(url())

    assert response.status_code == 200 and response.data['state'] == 'retired'
    assert NAME not in listed(staff)
    assert NAME not in descriptors(staff)
    assert NAME not in staff.get(WRITABLE).data
    assert NAME not in [d['field_name'] for d in staff.get(FIELD_MAPPINGS + '?tab=disease').data]
    grade.refresh_from_db()
    assert grade.retired_at is not None and grade.mapping.status == 'rejected'
    # An edit of the retired field is ignored, as a key with no definition always was.
    patch(staff, record, {NAME: '1'})
    assert NAME not in stored(record)


def test_a_retired_field_leaves_the_concept_to_code_and_scope_readers(staff, grade):
    from omop_core.services import disease_profiles
    assert disease_profiles.memberships().get(NAME) == ['MM']
    assert NAME in disease_profiles.field_names()
    staff.delete(url())
    assert NAME not in disease_profiles.memberships()
    assert NAME not in disease_profiles.extension_fields('MM')
    assert NAME not in disease_profiles.field_names()
    # The rows are kept, so a restore returns the field to the same disease.
    assert disease_profiles.memberships(include_retired=True)[NAME] == ['MM']


def test_retiring_keeps_stored_values_the_facts_the_answers_and_the_definition(staff, grade, record):
    patch(staff, record, {NAME: '1'})
    assert stored(record) == {NAME: '1'}
    fact = Observation.objects.get(person=record.person, observation_source_value=f'custom:{NAME}')

    staff.delete(url())

    assert stored(record) == {NAME: '1'}
    assert Observation.objects.filter(pk=fact.pk).exists()
    assert FieldChoice.objects.filter(field_name=NAME).count() == 2
    assert FieldDiseaseMembership.objects.filter(field_name=NAME).count() == 1
    assert CustomPatientField.objects.filter(pk=grade.pk).exists()


def test_a_derivation_carries_a_retired_fields_value_over_unchanged(staff, grade, record):
    patch(staff, record, {NAME: '1'})
    staff.delete(url())

    refresh_patient_record(record.person)
    refresh_patient_record(record.person)

    assert stored(record) == {NAME: '1'}


def test_a_derivation_still_drops_a_key_no_field_defines(staff, grade, record):
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={'never_defined': 'x'})
    refresh_patient_record(record.person)
    assert 'never_defined' not in stored(record)


def test_retiring_twice_is_harmless(staff, grade):
    first = staff.delete(url())
    again = staff.delete(url())
    assert first.status_code == again.status_code == 200
    assert again.data['state'] == 'retired'


def test_the_removed_fields_are_listed_for_an_admin_with_their_scope(staff, grade):
    staff.delete(url())
    response = staff.get(CUSTOM + '?state=retired')
    assert response.status_code == 200
    assert [(f['field_name'], f['diseases'], f['retired_at'] is not None) for f in response.data] == [
        (NAME, ['MM'], True)]


def test_a_retired_dropdowns_answers_cannot_be_edited_through_the_answer_editor(staff, grade):
    staff.delete(url())
    response = staff.post('/api/v1/field-choices/', {'field_name': NAME, 'display': 'Grade group 3'},
                          format='json')
    assert response.status_code == 400


def test_a_retired_computed_fields_formula_does_not_run(staff, concepts, record):
    make(staff, concepts, field_name='score_plus_one', field_type='number', mode='computed',
         formula='1 + 1', answers=[], diseases=[], concept=concepts['question'].concept_id)
    FieldFormula.objects.filter(field_name='score_plus_one').update(is_active=True)
    refresh_patient_record(record.person)
    assert stored(record).get('score_plus_one') == 2
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={'score_plus_one': 99})
    staff.delete(url('score_plus_one'))
    refresh_patient_record(record.person)
    assert stored(record) == {'score_plus_one': 99}


# ------------------------------------------------------------------ restoring


def test_restore_brings_back_the_field_its_scope_answers_and_values(staff, grade, record):
    patch(staff, record, {NAME: '2'})
    staff.delete(url())

    response = staff.post(url(suffix='restore/'))

    assert response.status_code == 200 and response.data['retired_at'] is None
    assert response.data['diseases'] == ['MM']
    assert [o['value'] for o in response.data['options']] == ['1', '2']
    assert NAME in listed(staff) and NAME in descriptors(staff) and NAME in staff.get(WRITABLE).data
    grade.refresh_from_db()
    assert grade.mapping.status == 'approved'
    assert stored(record) == {NAME: '2'}
    patch(staff, record, {NAME: '1'})
    assert stored(record) == {NAME: '1'}
    assert NAME not in staff.get(CUSTOM + '?state=retired').data and not staff.get(CUSTOM + '?state=retired').data


def test_restoring_a_field_that_is_not_removed_is_refused(staff, grade):
    response = staff.post(url(suffix='restore/'))
    assert response.status_code == 409 and response.data['code'] == 'not_retired'


def test_restore_refuses_when_another_approved_mapping_took_its_key(staff, grade):
    staff.delete(url())
    mapping = FieldConceptMapping.objects.get(pk=grade.mapping_id)
    FieldConceptMapping.objects.create(
        field_name='some_other_field', status='approved',
        omop_table=mapping.omop_table, source_value=mapping.source_value)
    response = staff.post(url(suffix='restore/'))
    assert response.status_code == 409 and response.data['code'] == 'mapping_conflict'
    assert CustomPatientField.objects.get(pk=grade.pk).retired_at is not None


def test_creating_a_field_under_a_removed_name_points_to_restore(staff, grade, concepts):
    staff.delete(url())
    response = make(staff, concepts, expect=400)
    assert 'Restore' in str(response.data['field_name'])


def test_restore_does_not_reproject_the_records_values(staff, grade, record, monkeypatch):
    patch(staff, record, {NAME: '1'})
    staff.delete(url())
    calls = []
    monkeypatch.setattr('omop_core.signals._project_field_sync', lambda pk: calls.append(pk))
    staff.post(url(suffix='restore/'))
    assert calls == []
    assert Observation.objects.filter(person=record.person).count() == 1


# ------------------------------------------------------------- the usage read


def test_usage_counts_the_records_holding_a_value(staff, grade):
    one, two, empty = PatientRecordFactory(), PatientRecordFactory(), PatientRecordFactory()
    patch(staff, one, {NAME: '1'})
    patch(staff, two, {NAME: '2'})
    PatientRecord.objects.filter(pk=empty.pk).update(custom_fields={NAME: None})

    data = staff.get(url()).data

    assert data['records_with_value'] == 2 and data['has_facts'] is True
    assert data['answers'] == 2 and data['can_delete'] is False and data['retired'] is False


def test_usage_of_an_unused_field_says_it_can_be_deleted(staff, grade):
    data = staff.get(url()).data
    assert data['records_with_value'] == 0 and data['has_facts'] is False and data['can_delete'] is True


# ------------------------------------------------------------- hard deleting


def test_permanent_delete_is_refused_while_a_record_holds_a_value(staff, grade, record):
    patch(staff, record, {NAME: '1'})

    response = staff.delete(url() + '?permanent=true')

    assert response.status_code == 409
    assert response.data['code'] == 'has_data' and response.data['records_with_value'] == 1
    assert response.data['can_delete'] is False
    assert CustomPatientField.objects.filter(pk=grade.pk).exists()
    assert stored(record) == {NAME: '1'}


def test_permanent_delete_is_refused_while_facts_exist_under_the_fields_own_key(staff, grade, concepts):
    person_record = PatientRecordFactory()
    ObservationFactory(person=person_record.person, observation_concept=concepts['question'],
                       observation_source_value=f'custom:{NAME}')

    response = staff.delete(url() + '?permanent=true')

    assert response.status_code == 409 and response.data['has_facts'] is True
    assert response.data['records_with_value'] == 0


def test_a_lab_at_the_fields_concept_does_not_block_a_permanent_delete(staff, grade, concepts):
    """A patient's own result at the concept exists with or without the field (#2930)."""
    person_record = PatientRecordFactory()
    fact = ObservationFactory(person=person_record.person, observation_concept=concepts['question'],
                              observation_source_value='LOINC-CODE')
    assert staff.get(url()).data['can_delete'] is True

    response = staff.delete(url() + '?permanent=true')

    assert response.status_code == 200 and response.data['state'] == 'deleted'
    assert not CustomPatientField.objects.filter(field_name=NAME).exists()
    assert Observation.objects.filter(pk=fact.pk).exists()


def test_permanent_delete_of_an_unused_field_removes_it_and_everything_keyed_to_it(staff, grade, concepts):
    record = PatientRecordFactory()
    # A cleared value leaves a pending pin and nothing else; a rejection names the key.
    PatientRecord.objects.filter(pk=record.pk).update(
        user_edited_fields=[f'custom_fields.{NAME}', 'disease'])
    ProposalRejection.objects.create(person=record.person, field_name=f'custom_fields.{NAME}', origins=[])
    mapping_id = grade.mapping_id
    assert not Observation.objects.exists()

    response = staff.delete(url() + '?permanent=true')

    assert response.status_code == 200 and response.data['state'] == 'deleted'
    assert not CustomPatientField.objects.filter(field_name=NAME).exists()
    assert not FieldConceptMapping.objects.filter(pk=mapping_id).exists()
    assert not FieldChoice.objects.filter(field_name=NAME).exists()
    assert not FieldDiseaseMembership.objects.filter(field_name=NAME).exists()
    assert not ProposalRejection.objects.filter(field_name=f'custom_fields.{NAME}').exists()
    assert PatientRecord.objects.get(pk=record.pk).user_edited_fields == ['disease']
    assert NAME not in listed(staff) and NAME not in descriptors(staff)


def test_a_deleted_name_can_be_created_again(staff, grade, concepts):
    staff.delete(url() + '?permanent=true')
    make(staff, concepts)
    assert CustomPatientField.objects.filter(field_name=NAME).count() == 1
    assert FieldChoice.objects.filter(field_name=NAME).count() == 2


def test_a_retired_field_can_be_deleted_later_once_nothing_depends_on_it(staff, grade):
    staff.delete(url())
    response = staff.delete(url() + '?permanent=true')
    assert response.status_code == 200 and response.data['state'] == 'deleted'
    assert not FieldConceptMapping.objects.filter(pk=grade.mapping_id).exists()


def test_a_retired_field_with_values_cannot_be_deleted(staff, grade, record):
    patch(staff, record, {NAME: '1'})
    staff.delete(url())
    assert staff.delete(url() + '?permanent=true').status_code == 409
    assert stored(record) == {NAME: '1'}


def test_deleting_a_text_field_with_no_concept_facts(staff, concepts):
    make(staff, concepts, field_name='free_note', field_type='text', answers=[], diseases=[])
    response = staff.delete(url('free_note') + '?permanent=true')
    assert response.status_code == 200
    assert not CustomPatientField.objects.filter(field_name='free_note').exists()


def test_the_service_refuses_a_delete_it_finds_data_for(grade, record):
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={NAME: '1'})
    with pytest.raises(custom_field_removal.RemovalError) as error:
        custom_field_removal.delete(grade)
    assert error.value.code == 'has_data' and error.value.extra['records_with_value'] == 1


# ------------------------------------------------------------ patients' reads


def test_a_patient_no_longer_sees_a_retired_field(staff, grade, record):
    identity = Identity.objects.create_user(email='patient@example.org')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)
    assert NAME in listed(client)
    staff.delete(url())
    assert NAME not in listed(client)


# ------------------------------------------------------- copying between instances


def test_a_removed_field_travels_as_removed(staff, grade):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    staff.delete(url())
    payload = read_payload('default')
    assert [c['retired'] for c in payload['custom_fields']] == [True]

    FieldDiseaseMembership.objects.all().delete()
    CustomPatientField.objects.all().delete()
    FieldConceptMapping.objects.all().delete()
    apply_payload(payload)

    copy = CustomPatientField.objects.get(field_name=NAME)
    assert copy.retired_at is not None and copy.mapping.status == 'rejected'
    # A payload with no ``retired`` key (written before this) leaves the state alone.
    payload['custom_fields'][0].pop('retired')
    apply_payload(payload)
    assert CustomPatientField.objects.get(field_name=NAME).retired_at is not None


def test_a_retired_fields_pending_pin_survives_a_derivation_and_a_restore(staff, grade, record):
    PatientRecord.objects.filter(pk=record.pk).update(
        user_edited_fields=[f'custom_fields.{NAME}', 'disease'], custom_fields={NAME: '1'})
    staff.delete(url())

    refresh_patient_record(record.person)

    kept = PatientRecord.objects.get(pk=record.pk)
    assert f'custom_fields.{NAME}' in kept.user_edited_fields
    assert kept.custom_fields == {NAME: '1'}


# ------------------------------------------------------------ review follow-ups


@pytest.mark.parametrize('name', ['psa__level', 'psa__isnull', 'contains', 'x__icontains', 'in'])
def test_a_name_that_looks_like_a_lookup_is_counted_as_the_key_it_is(staff, concepts, name):
    make(staff, concepts, field_name=name, field_type='text', answers=[], diseases=[])
    record = PatientRecordFactory()
    PatientRecord.objects.filter(pk=record.pk).update(custom_fields={name: 'x'})

    data = staff.get(url(name)).data

    assert data['records_with_value'] == 1 and data['can_delete'] is False
    assert staff.delete(url(name) + '?permanent=true').status_code == 409
    assert staff.delete(url(name)).status_code == 200


def test_a_field_another_request_deleted_is_a_404_not_a_500(grade):
    stale = CustomPatientField.objects.get(pk=grade.pk)
    custom_field_removal.delete(grade)
    with pytest.raises(custom_field_removal.RemovalError) as error:
        custom_field_removal.retire(stale)
    assert error.value.code == 'gone'


def test_permanent_delete_retires_first_and_a_refusal_leaves_it_retired(staff, grade, record, monkeypatch):
    # A value lands after the first check (a write that resolved the field while active).
    original = custom_field_removal.usage
    calls = []

    def racing(field):
        calls.append(field.retired_at is not None)
        result = original(field)
        if len(calls) == 1:
            # After the first check has passed, before the field is retired.
            PatientRecord.objects.filter(pk=record.pk).update(custom_fields={NAME: '1'})
        return result

    monkeypatch.setattr(custom_field_removal, 'usage', racing)
    response = staff.delete(url() + '?permanent=true')

    assert response.status_code == 409 and response.data['code'] == 'has_data'
    grade.refresh_from_db()
    assert grade.retired_at is not None
    assert stored(record) == {NAME: '1'}


def test_a_removed_fields_scope_cannot_be_changed(staff, grade):
    staff.delete(url())
    response = staff.put(f'{FIELD_MAPPINGS}{NAME}/diseases/', {'diseases': []}, format='json')
    assert response.status_code == 409 and response.data['code'] == 'retired_field'
    assert FieldDiseaseMembership.objects.filter(field_name=NAME).count() == 1


@pytest.mark.parametrize('new_status', ['proposed', 'rejected'])
def test_an_active_created_fields_mapping_cannot_be_unapproved_by_hand(staff, grade, new_status):
    response = staff.patch(f'{FIELD_MAPPINGS}{grade.mapping_id}/', {'status': new_status}, format='json')
    assert response.status_code == 409 and response.data['code'] == 'created_field'
    assert FieldConceptMapping.objects.get(pk=grade.mapping_id).status == 'approved'


def test_a_removed_fields_scope_survives_copying_to_another_instance(staff, grade):
    from omop_core.services.field_curation_transfer import apply_payload, read_payload

    staff.delete(url())
    payload = read_payload('default')
    FieldDiseaseMembership.objects.all().delete()
    CustomPatientField.objects.all().delete()
    FieldConceptMapping.objects.all().delete()
    apply_payload(payload)
    assert list(FieldDiseaseMembership.objects.filter(field_name=NAME).values_list('disease', flat=True)) == ['MM']
