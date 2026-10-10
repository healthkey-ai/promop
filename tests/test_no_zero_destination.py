"""Concept 0 ("No matching concept") is never a mapping destination.

A code nothing fits stays unmapped: no destination, not destination 0.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture()
def concepts():
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    vocabulary = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    klass = ConceptClassFactory(concept_class_id='Clinical Observation')
    no_match = ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='No matching concept',
                              vocabulary=vocabulary, domain=domain, concept_class=klass, standard_concept='')
    rr = ConceptFactory(concept_id=3024171, concept_name='Respiratory rate', concept_code='9279-1',
                        vocabulary=vocabulary, domain=domain, concept_class=klass, standard_concept='S')
    return no_match, rr


def _hit(concept_id, name):
    return {'concept_id': concept_id, 'concept_name': name, 'concept_code': str(concept_id),
            'vocabulary_id': 'LOINC', 'concept_class_id': 'Clinical Observation',
            'domain_id': 'Measurement', 'retrieval': 'lexical', 'lexical_score': 0.9}


def test_retrieval_never_offers_concept_zero(monkeypatch):
    from omop_core.mapping.suggestions import retrieval_pool

    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates',
                        lambda *a: ([_hit(0, 'No matching concept')], 'C0'))
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates',
                        lambda *a, **k: [_hit(0, 'No matching concept'), _hit(3024171, 'Respiratory rate')])
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates',
                        lambda *a, **k: [{**_hit(0, 'No matching concept'), 'semantic_score': 0.8}])
    candidates, _, _ = retrieval_pool(
        source_code='RR', source_vocabulary_id='', source_text='respirations',
        domain_id='Measurement', strategies=['umls', 'lexical', 'vectors'],
    )
    assert [c['concept_id'] for c in candidates] == [3024171]


def test_batch_suggest_leaves_the_code_unmapped_when_only_zero_fits(concepts, monkeypatch):
    from omop_core.services.mapping_suggestions import suggest_mappings

    mapping = SourceCodeConceptMapping.objects.create(
        source_code='RR', source_vocabulary_id='', omop_table='measurement', domain_id='Measurement',
        status='proposed', source_code_description='respirations', occurrence_count=5,
    )
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))
    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates',
                        lambda *a, **k: [_hit(0, 'No matching concept')])
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *a, **k: [])
    # Even a ranker that would pick the first candidate has nothing to pick.
    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates',
                        lambda value, candidates, *a, **k: (candidates[0] if candidates else None, 'x', []))
    suggest_mappings('measurement', min_occurrences=1)
    mapping.refresh_from_db()
    assert mapping.target_concept_id is None
    assert mapping.status == 'proposed'


class TestApi:
    @pytest.fixture(autouse=True)
    def _client(self, concepts):
        from patient_portal.models import Identity
        self.client = APIClient()
        self.client.force_authenticate(Identity.objects.create_user(email='zero@test.com', is_staff=True))

    @pytest.mark.parametrize('key', ['destination_concept_id', 'target_concept_id', 'concept_id'])
    @pytest.mark.parametrize('zero', [0, '0'])
    def test_creating_with_zero_is_refused(self, key, zero):
        response = self.client.post('/api/v1/code-mappings/', {
            'source_vocabulary_id': 'LOINC', 'source_code': '9279-1', 'domain_id': 'Measurement',
            key: zero,
        }, format='json')
        assert response.status_code == 400
        assert 'No matching concept' in str(response.data)
        assert not SourceCodeConceptMapping.objects.exists()

    def test_saving_a_row_without_a_destination_keeps_it_empty(self):
        mapping = SourceCodeConceptMapping.objects.create(
            source_code='RR', source_vocabulary_id='', omop_table='measurement',
            domain_id='Measurement', status='proposed',
        )
        response = self.client.patch(f'/api/v1/code-mappings/{mapping.pk}/', {
            'destination_concept_id': None, 'target_concept_id': None, 'notes': 'needs a curator',
        }, format='json')
        assert response.status_code == 200, response.data
        mapping.refresh_from_db()
        assert mapping.target_concept_id is None
        assert mapping.notes == 'needs a curator'
