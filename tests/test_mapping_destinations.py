import pytest
from rest_framework.test import APIClient

from omop_core.models import MappingDestinationCandidate, SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def mapping_setup(django_user_model):
    domain = DomainFactory(domain_id='Condition')
    vocab = VocabularyFactory(vocabulary_id='SNOMED')
    targets = [ConceptFactory(concept_id=i, concept_code=str(i), concept_name=f'Target {i}',
                              vocabulary=vocab, domain=domain, standard_concept='S') for i in (111, 222)]
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code='A01', domain_id='Condition',
        omop_table='condition', status='proposed', occurrence_count=42,
    )
    for concept in targets:
        MappingDestinationCandidate.objects.create(mapping=mapping, target_concept=concept,
            target_vocabulary_id='SNOMED', target_concept_code=concept.concept_code, origins=['HT-One'])
    MappingDestinationCandidate.objects.create(mapping=mapping,
        target_vocabulary_id='SNOMED', target_concept_code='999', origins=['HT-One'])
    client = APIClient()
    client.force_authenticate(django_user_model.objects.create_user(email='curator@test.com', is_staff=True))
    return client, mapping, targets


def test_counts_include_unloaded_alternatives_and_ignore_search_filter(mapping_setup):
    client, mapping, targets = mapping_setup
    mapping.target_concept = targets[0]
    mapping.save()
    response = client.get('/api/v1/code-mappings/', {'search': 'Target 111'})
    assert response.status_code == 200
    assert response.data[0]['destination_count'] == 3
    gap = SourceCodeConceptMapping.objects.create(source_vocabulary_id='ICD10', source_code='GAP')
    rows = client.get('/api/v1/code-mappings/').data
    assert next(row for row in rows if row['mapping_id'] == gap.pk)['destination_count'] == 0


def test_curator_selects_alternative_and_reopens_without_losing_candidates(mapping_setup):
    client, mapping, targets = mapping_setup
    url = f'/api/v1/code-mappings/{mapping.pk}/'
    detail = client.get(url)
    assert detail.data['destination_count'] == 3
    options = detail.data['destination_options']
    assert len(options) == 3
    assert next(o for o in options if o['concept_code'] == '999')['selectable'] is False
    response = client.patch(url, {'destination_concept_id': targets[1].pk, 'status': 'approved'}, format='json')
    assert response.status_code == 200, response.data
    assert response.data['destination_count'] == 3
    mapping.refresh_from_db()
    assert mapping.target_concept_id == targets[1].pk
    assert mapping.status == 'approved'
    assert mapping.destination_candidates.count() == 3
    reopened = client.get(url).data
    assert [o['concept_id'] for o in reopened['destination_options'] if o['selected']] == [222]


def test_selected_destination_outside_import_is_counted_once(mapping_setup):
    client, mapping, targets = mapping_setup
    other = ConceptFactory(concept_id=333, concept_code='333', vocabulary=targets[0].vocabulary,
                           domain=targets[0].domain, standard_concept='S')
    mapping.target_concept = other
    mapping.save()
    assert client.get('/api/v1/code-mappings/').data[0]['destination_count'] == 4
    assert client.get(f'/api/v1/code-mappings/{mapping.pk}/').data['destination_count'] == 4


def test_candidates_cannot_be_reassigned_to_a_different_source(mapping_setup):
    client, mapping, _ = mapping_setup
    response = client.patch(f'/api/v1/code-mappings/{mapping.pk}/', {'source_code': 'A02'}, format='json')
    assert response.status_code == 400
    mapping.refresh_from_db()
    assert mapping.source_code == 'A01'


def test_patient_cannot_read_curator_candidates(mapping_setup, django_user_model):
    client, mapping, _ = mapping_setup
    client.force_authenticate(django_user_model.objects.create_user(email='patient@test.com'))
    assert client.get(f'/api/v1/code-mappings/{mapping.pk}/').status_code == 403
