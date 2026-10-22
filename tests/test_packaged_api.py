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
