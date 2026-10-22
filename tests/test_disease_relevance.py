"""Disease relevance sets, the patient-disease read and curator signals (#1942)."""
import re
from pathlib import Path

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping, GroupAccess, RelevanceSignal
from omop_core.services import disease_relevance
from omop_core.services.genomics_catalog import markers
from tests.factories import (
    ConceptFactory, ConditionOccurrenceFactory, OrganizationFactory, PatientRecordFactory,
)

pytestmark = pytest.mark.django_db

URL = '/api/v1/relevance/'
SIGNALS = '/api/v1/relevance/signals/'
GRANTS = {
    'cdew': {'token': 'cdew-test-secret-000000000000000000', 'scopes': 'system/*.read system/etl.write'},
    'cdew-disease': {'token': 'disease-test-secret-0000000000000000', 'scopes': 'system/disease.read'},
    'reference-only': {'token': 'reference-test-secret-00000000000000', 'scopes': 'system/*.read'},
}


def service(name):
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f'Bearer {GRANTS[name]["token"]}')
    return api


def approve(field, concept=None, status='approved', omop_table='measurement'):
    concept = concept or ConceptFactory(concept_name=field.replace('_', ' '))
    FieldConceptMapping.objects.create(field_name=field, concept=concept, status=status, omop_table=omop_table)
    return concept


def test_disease_fields_match_what_the_disease_tab_renders():
    source = (Path(__file__).resolve().parent.parent /
              'frontend/src/components/PatientInfo/tabs/DiseaseTab.tsx').read_text()
    sections = dict(zip(*[iter(re.split(r'\nfunction (\w+)\(', source)[1:])] * 2))

    def fields(*names):
        return {a or b or c for name in names for a, b, c in re.findall(
            r'\bname="(\w+)"|data-patient-field="(\w+)"|field\(\'[^\']+\', \'(\w+)\'', sections[name])}

    assert disease_relevance.DISEASE_FIELDS == {
        'BC': fields('BreastCancerSection', 'StagingBiomarkersSection'),
        'FL': fields('LymphomaSection'),
        'MM': fields('MyelomaSection'),
        'CLL': fields('CLLSection'),
        'MCL': fields('MCLSection'),
    }


def test_set_holds_approved_disease_fields_common_fields_and_genomics():
    flc = approve('kappa_flc')
    hgb = approve('hemoglobin_g_dl')
    approve('lambda_flc', status='proposed')
    approve('binet_stage')  # CLL's, not myeloma's
    marker = next(m for m in markers() if 'MM' in m['diseases'])
    gene = approve(marker['field_name'])

    body = disease_relevance.relevance(['MM'])

    by_field = {c['field_name']: c for c in body['concepts']}
    assert by_field['kappa_flc']['tier'] == 'disease'
    assert by_field[marker['field_name']]['concept_id'] == gene.concept_id
    assert by_field['hemoglobin_g_dl']['tier'] == 'common'
    assert by_field['hemoglobin_g_dl']['concept_id'] == hgb.concept_id
    assert by_field['kappa_flc']['concept_id'] == flc.concept_id
    assert 'binet_stage' not in by_field
    assert 'lambda_flc' not in by_field
    assert 'lambda_flc' in body['unmapped_fields']['MM']
    # Disease concepts lead, so an extractor can rank by position as well as tier.
    assert body['concepts'][0]['tier'] == 'disease'


def test_union_of_diseases_lists_each_concept_once_with_its_diseases():
    approve('stage')
    approve('hemoglobin_g_dl')

    body = disease_relevance.relevance(['MM', 'FL'])

    stage = [c for c in body['concepts'] if c['field_name'] == 'stage']
    assert len(stage) == 1 and stage[0]['diseases'] == ['MM', 'FL'] and stage[0]['tier'] == 'disease'


def test_version_changes_with_curation_and_not_otherwise():
    before = disease_relevance.current_version()
    assert disease_relevance.current_version() == before
    approve('kappa_flc')
    assert disease_relevance.current_version() != before


@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_endpoint_is_versioned_cacheable_and_readable_with_reference_scope():
    concept = approve('kappa_flc')
    from omop_core.models import ConceptSynonym
    ConceptSynonym.objects.create(concept=concept, concept_synonym_name='Free kappa',
                                  language_concept=ConceptFactory(concept_name='English'))
    api = service('reference-only')

    response = api.get(URL, {'disease': 'multiple myeloma'})

    assert response.status_code == 200
    assert response.data['diseases'] == ['MM']
    entry = next(c for c in response.data['concepts'] if c['field_name'] == 'kappa_flc')
    assert entry['synonyms'] == ['Free kappa'] and entry['omop_table'] == 'measurement'
    assert response['ETag'] == f'"rel-{response.data["version"]}-MM"'
    assert api.get(URL, {'disease': 'MM'}, HTTP_IF_NONE_MATCH=response['ETag']).status_code == 304
    approve('lambda_flc')
    assert api.get(URL, {'disease': 'MM'}, HTTP_IF_NONE_MATCH=response['ETag']).status_code == 200


@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_endpoint_rejects_unknown_or_missing_disease():
    api = service('reference-only')
    assert api.get(URL, {'disease': 'glioma'}).status_code == 400
    assert api.get(URL).status_code == 400


@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_patient_diseases_need_the_disease_scope():
    record = PatientRecordFactory(disease='Multiple myeloma')
    pid = record.person.person_id

    assert service('reference-only').get(URL, {'person_id': pid}).status_code == 403
    response = service('cdew-disease').get(URL, {'person_id': pid})
    assert response.status_code == 200
    assert response.data['person_id'] == pid and 'MM' in response.data['diseases']
    # The disease scope reads only that: the catalog by disease is reference data.
    assert service('cdew-disease').get(URL, {'disease': 'MM'}).status_code == 200
    assert service('cdew-disease').get(f'/api/v1/patient-records/{pid}/').status_code == 403
    assert service('cdew-disease').get(URL, {'person_id': 999_999_999}).status_code == 404


def test_patient_with_two_cancers_gets_both():
    record = PatientRecordFactory(disease='Chronic lymphocytic leukemia')
    for name in ('Chronic lymphocytic leukemia', 'Multiple myeloma'):
        ConditionOccurrenceFactory(person=record.person, condition_concept=ConceptFactory(concept_name=name))

    diseases = disease_relevance.patient_diseases(record.person)

    assert set(diseases) == {'CLL', 'MM'}


def test_patient_with_no_supported_cancer_has_no_diseases():
    record = PatientRecordFactory(disease='Glioblastoma')
    assert disease_relevance.patient_diseases(record.person) == []


def test_patient_disease_read_by_a_session_user_follows_patient_access(django_user_model):
    record = PatientRecordFactory(disease='Multiple myeloma')
    stranger = django_user_model.objects.create_user(email='stranger@test.com')
    api = APIClient()
    api.force_authenticate(stranger)
    assert api.get(URL, {'person_id': record.person.person_id}).status_code == 403
    staff = django_user_model.objects.create_user(email='staff@test.com', is_staff=True)
    api.force_authenticate(staff)
    assert api.get(URL, {'person_id': record.person.person_id}).status_code == 200


@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_signals_are_received_upserted_and_validated():
    concept = ConceptFactory()
    api = service('cdew')
    row = {'disease': 'MM', 'concept_id': concept.concept_id, 'document_count': 31,
           'document_total': 100, 'period': '2026-10'}

    assert api.post(SIGNALS, [row], format='json').data == {'created': 1, 'updated': 0}
    assert api.post(SIGNALS, {**row, 'document_count': 40}, format='json').data == {'created': 0, 'updated': 1}
    signal = RelevanceSignal.objects.get()
    assert (signal.source, signal.document_count, signal.status) == ('cdew', 40, 'proposed')

    assert api.post(SIGNALS, [{**row, 'concept_id': 1}], format='json').status_code == 400
    assert api.post(SIGNALS, [{**row, 'disease': 'glioma'}], format='json').status_code == 400
    assert api.post(SIGNALS, [{**row, 'document_total': 5}], format='json').status_code == 400
    # Reading the queue is for curators, not the reporting service.
    assert api.get(SIGNALS).status_code == 403
    assert service('reference-only').post(SIGNALS, [row], format='json').status_code == 403


@override_settings(SERVICE_AUTH_TOKENS=GRANTS, SERVICE_AUTH_TOKEN='')
def test_accepted_signal_joins_the_set_and_decisions_carry_to_later_periods(django_user_model):
    concept = ConceptFactory(concept_name='Bone marrow aspirate')
    service('cdew').post(SIGNALS, [{'disease': 'MM', 'concept_id': concept.concept_id,
                                    'document_count': 30, 'period': '2026-09'}], format='json')
    signal = RelevanceSignal.objects.get()
    org = OrganizationFactory()
    doctor = django_user_model.objects.create_user(email='doctor@test.com')
    GroupAccess.objects.create(identity=doctor, org=org, role='doctor')
    admin = django_user_model.objects.create_user(email='admin@test.com')
    GroupAccess.objects.create(identity=admin, org=org, role='org_admin')
    curator = APIClient()
    curator.force_authenticate(doctor)
    queue = curator.get(SIGNALS).data['results']
    assert [(s['id'], s['concept_name']) for s in queue] == [(signal.id, 'Bone marrow aspirate')]
    assert curator.patch(f'{SIGNALS}{signal.id}/', {'status': 'accepted'}, format='json').status_code == 403

    before = disease_relevance.current_version()
    curator.force_authenticate(admin)
    assert curator.patch(f'{SIGNALS}{signal.id}/', {'status': 'accepted'}, format='json').data['status'] == 'accepted'

    curated = [c for c in disease_relevance.relevance(['MM'])['concepts'] if c['tier'] == 'curated']
    assert [c['concept_id'] for c in curated] == [concept.concept_id]
    assert disease_relevance.relevance(['FL'])['concepts'] == []
    assert disease_relevance.current_version() != before

    service('cdew').post(SIGNALS, [{'disease': 'MM', 'concept_id': concept.concept_id,
                                    'document_count': 12, 'period': '2026-10'}], format='json')
    assert set(RelevanceSignal.objects.values_list('status', flat=True)) == {'accepted'}
    assert curator.get(SIGNALS).data['results'] == []
