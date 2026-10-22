"""The endpoints the PROMOP widget calls, served from the packaged wheel."""
from datetime import date

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from omop_core.models import Concept, DrugExposure, PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record
from omop_oncology.models import Episode
from patient_portal.models import Identity
from tests.factories import ConceptFactory, PatientRecordFactory, VocabularyFactory
from tests.test_episode_service import _seed_episode_writer_concepts

pytestmark = pytest.mark.django_db

PACKAGED = dict(ROOT_URLCONF='tests.urls_packaged_api')
START = date(2025, 1, 15)


@pytest.fixture
def api():
    identity = Identity.objects.create_user(email='packaged-api@example.test')
    client = APIClient()
    client.force_authenticate(identity)
    return client


@pytest.fixture
def person():
    _seed_episode_writer_concepts()
    return PatientRecordFactory().person


def test_the_endpoints_require_authentication():
    with override_settings(**PACKAGED):
        assert APIClient().get('/federation/v1/therapy-outcomes/').status_code in (401, 403)
        assert APIClient().post('/federation/v1/therapy-lines/', {}, format='json').status_code in (401, 403)


@override_settings(**PACKAGED)
def test_outcomes_come_from_the_packaged_catalog(api):
    response = api.get('/federation/v1/therapy-outcomes/', {'disease': 'Multiple Myeloma'})
    assert response.status_code == 200
    assert any(item['value'] == 'Very Good Partial Response' for item in response.data)


@override_settings(**PACKAGED)
def test_regimens_list_and_detail_expose_components_and_classes(api):
    vocab = VocabularyFactory(vocabulary_id='HemOnc', vocabulary_name='HemOnc')
    concept = ConceptFactory(concept_name='Bortezomib', vocabulary=vocab)
    from omop_core.models import TherapyComponent, TherapyRegimen, TherapyRegimenComponent
    regimen = TherapyRegimen.objects.create(code='packaged-vrd', title='VRd')
    component = TherapyComponent.objects.create(
        code='bortezomib', title='Bortezomib', concept=concept)
    TherapyRegimenComponent.objects.create(regimen=regimen, component=component)

    listed = api.get('/federation/v1/therapy-regimens/', {'search': 'VR'})
    assert listed.status_code == 200
    assert {'code': 'packaged-vrd', 'title': 'VRd', 'concept_id': None} in listed.data

    detail = api.get('/federation/v1/therapy-regimens/packaged-vrd/')
    assert detail.status_code == 200
    assert detail.data['code'] == 'packaged-vrd'
    assert detail.data['components'][0]['code'] == 'bortezomib'
    assert detail.data['components'][0]['concept_id'] == concept.pk


@override_settings(**PACKAGED)
def test_regimen_detail_is_404_for_an_unknown_code(api):
    assert api.get('/federation/v1/therapy-regimens/nope/').status_code == 404


@override_settings(**PACKAGED)
def test_concept_search_keeps_the_three_character_minimum(api):
    assert api.get('/federation/v1/concepts/search/', {'q': 'le'}).status_code == 400

    vocab = VocabularyFactory(vocabulary_id='RxNorm', vocabulary_name='RxNorm')
    ConceptFactory(concept_name='lenalidomide', vocabulary=vocab, standard_concept='S',
                   concept_code='337535')
    response = api.get('/federation/v1/concepts/search/', {
        'q': 'lenalidomide', 'vocabulary_id': 'RxNorm'})
    assert response.status_code == 200
    names = [item['concept_name'] for item in response.data['results']]
    assert 'lenalidomide' in names


@override_settings(**PACKAGED)
def test_authoring_a_line_through_the_packaged_endpoint(api, person):
    drug = ConceptFactory(concept_name='Bortezomib')
    response = api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': drug.pk, 'source_value': 'bortezomib'}],
    }, format='json')

    assert response.status_code == 201, response.data
    assert response.data['line_number'] == 1
    assert len(response.data['drug_exposure_ids']) == 1
    assert response.data['patient_info'] is None          # host supplies its own shape
    record = PatientRecord.objects.get(person=person)
    assert record.therapy_lines_count == 1
    assert record.first_line_therapy == 'bortezomib'


@override_settings(**PACKAGED)
def test_editing_a_line_through_the_packaged_endpoint(api, person):
    drug = ConceptFactory(concept_name='Bortezomib')
    created = api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': drug.pk, 'source_value': 'bortezomib'}],
        'outcome': 'Partial Response',
    }, format='json')
    episode_id = created.data['episode_id']

    edited = api.patch(f'/federation/v1/therapy-lines/{episode_id}/',
                       {'intent': 'Curative'}, format='json')
    assert edited.status_code == 200, edited.data
    assert edited.data['drug_exposure_ids'] == created.data['drug_exposure_ids']
    assert DrugExposure.objects.get(person=person).drug_exposure_start_date == START
    assert Episode.objects.get(pk=episode_id).episode_number == 1


@override_settings(**PACKAGED)
def test_editing_an_unknown_line_is_404(api):
    assert api.patch('/federation/v1/therapy-lines/999999/', {'intent': 'Curative'},
                     format='json').status_code == 404


@override_settings(**PACKAGED)
def test_packaged_endpoint_rederives_the_record(api, person):
    drug = ConceptFactory(concept_name='Bortezomib')
    api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': drug.pk}],
    }, format='json')
    record = refresh_patient_record(person)
    assert record.first_line_start_date == str(START)


@override_settings(**PACKAGED)
def test_recording_a_line_without_its_composition(api, person):
    # "This line happened; I don't know which drugs" is a real statement (#1924).
    response = api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [], 'composition_unknown': True,
    }, format='json')

    assert response.status_code == 201, response.data
    record = refresh_patient_record(person)
    assert record.therapy_lines_count == 1
    from omop_core.api.lines import lines_of_therapy
    line = lines_of_therapy(record)[0]
    assert line['composition_known'] is False
    assert line['drugs'] == []


@override_settings(**PACKAGED)
def test_an_accidentally_empty_payload_is_still_refused(api, person):
    response = api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(), 'drugs': [],
    }, format='json')
    assert response.status_code == 400, response.data
    assert not Episode.objects.filter(person=person).exists()


@override_settings(**PACKAGED)
def test_retracting_a_line_removes_it_from_history_but_keeps_its_drugs(api, person):
    drug = ConceptFactory(concept_name='Bortezomib')
    created = api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': drug.pk, 'source_value': 'bortezomib'}],
    }, format='json')
    assert created.status_code == 201, created.data

    deleted = api.delete(f"/federation/v1/therapy-lines/{created.data['episode_id']}/")
    assert deleted.status_code == 200, deleted.data
    record = refresh_patient_record(person)
    assert not record.therapy_lines_count
    assert DrugExposure.objects.filter(person=person).exists()   # the fact is not ours to delete


@override_settings(**PACKAGED)
def test_clearing_composition_keeps_the_line(api, person):
    drug = ConceptFactory(concept_name='Bortezomib')
    created = api.post('/federation/v1/therapy-lines/', {
        'person': person.pk, 'line_number': 1, 'start_date': START.isoformat(),
        'drugs': [{'concept_id': drug.pk, 'source_value': 'bortezomib'}],
    }, format='json')

    cleared = api.patch(f"/federation/v1/therapy-lines/{created.data['episode_id']}/",
                        {'drugs': [], 'composition_unknown': True}, format='json')
    assert cleared.status_code == 200, cleared.data
    record = refresh_patient_record(person)
    assert record.therapy_lines_count == 1
    from omop_core.api.lines import lines_of_therapy
    assert lines_of_therapy(record)[0]['composition_known'] is False


@override_settings(**PACKAGED)
def test_retracting_an_unknown_line_is_404(api):
    assert api.delete('/federation/v1/therapy-lines/999999/').status_code == 404


@override_settings(**PACKAGED)
def test_history_is_not_complete_until_the_patient_says_so(api, person):
    record = refresh_patient_record(person)
    assert record.therapy_history_complete is None

    response = api.post('/federation/v1/therapy-history/completeness/',
                        {'person': person.pk, 'complete': True}, format='json')

    assert response.status_code == 200, response.data
    assert response.data['therapy_history_complete'] is True
    assert PatientRecord.objects.get(person=person).therapy_history_complete is True


@override_settings(**PACKAGED)
def test_withdrawing_the_confirmation_returns_to_a_lower_bound(api, person):
    api.post('/federation/v1/therapy-history/completeness/',
             {'person': person.pk, 'complete': True}, format='json')

    response = api.post('/federation/v1/therapy-history/completeness/',
                        {'person': person.pk, 'complete': False}, format='json')

    assert response.status_code == 200
    assert response.data['therapy_history_complete'] is None
    assert PatientRecord.objects.get(person=person).therapy_history_complete is None


@override_settings(**PACKAGED)
def test_a_confirmation_needs_a_known_patient(api):
    response = api.post('/federation/v1/therapy-history/completeness/',
                        {'person': 999999, 'complete': True}, format='json')
    assert response.status_code == 400, response.data
