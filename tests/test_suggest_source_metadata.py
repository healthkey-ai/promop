"""Suggest gives the rankers the row's Source Metadata as context (#1782, #1786).

The motivating case: Epic adds LOINC 8716-3 "Vital signs" to every vital sign,
so a respirations Observation reaches Suggest described as "Vital signs". Its
metadata says "Respirations" and carries LOINC 9279-1 "Respiratory rate".
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from omop_core.mapping import source_metadata
from omop_core.mapping.suggestion_context import build_source_context
from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from omop_core.services.mapping_suggestions import suggest_mappings, suggest_one_mapping
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

EPIC = 'urn:oid:1.2.840.114350.1.13.143.2.7.2.707679'

RESPIRATIONS = {
    'fhir': {
        'resourceType': 'Observation',
        'code': {
            'text': 'Respirations',
            'coding': [
                {'code': '9', 'system': EPIC, 'display': 'Respirations'},
                {'code': 'tu1-AMRt7RdiowUM6SRsbog0',
                 'system': 'http://open.epic.com/FHIR/StructureDefinition/observation-flowsheet-id',
                 'display': 'Respirations'},
                {'code': '9279-1', 'system': 'urn:oid:1.2.246.537.6.96'},
                {'code': '8716-3', 'system': 'http://loinc.org', 'display': 'Vital signs'},
                {'code': '9279-1', 'system': 'http://loinc.org', 'display': 'Respiratory rate'},
            ],
        },
        'category': [{'coding': [{'code': 'vital-signs', 'display': 'Vital Signs'}]}],
        'valueQuantity': {'value': 16, 'unit': '/min'},
    },
}


@pytest.fixture()
def loinc():
    domain = DomainFactory(domain_id='Measurement', domain_name='Measurement')
    vocabulary = VocabularyFactory(vocabulary_id='LOINC', vocabulary_name='LOINC')
    lab = ConceptClassFactory(concept_class_id='Clinical Observation')
    respiratory_rate = ConceptFactory(
        concept_id=3024171, concept_name='Respiratory rate', concept_code='9279-1',
        vocabulary=vocabulary, domain=domain, concept_class=lab, standard_concept='S',
    )
    vital_signs = ConceptFactory(
        concept_id=3036277, concept_name='Vital signs', concept_code='8716-3',
        vocabulary=vocabulary, domain=domain, concept_class=lab, standard_concept='S',
    )
    return {'respiratory_rate': respiratory_rate, 'vital_signs': vital_signs,
            'vocabulary': vocabulary, 'domain': domain, 'class': lab}


class TestSummarize:
    def test_fhir_resource(self):
        summary = source_metadata.summarize([RESPIRATIONS])
        assert summary['texts'] == ['Respirations']
        assert summary['displays'] == ['Respirations', 'Vital signs', 'Respiratory rate']
        assert {'system': 'http://loinc.org', 'code': '9279-1',
                'display': 'Respiratory rate'} in summary['codings']
        assert len(summary['codings']) == 5
        assert summary['categories'] == ['Vital Signs']
        assert summary['units'] == ['/min']

    def test_etl_lists_and_other_fields(self):
        summary = source_metadata.summarize([
            {'display': ['Vital signs'], 'text': ['Blood Pressure'],
             'category': 'vital-signs', 'facilities': [{'name': 'A'}]},
            {'display': ['Vital signs'], 'text': ['Respirations', 'blood pressure']},
        ])
        assert summary['texts'] == ['Blood Pressure', 'Respirations']
        assert summary['displays'] == ['Vital signs']
        assert summary['other'] == {'category': 'vital-signs'}

    def test_resource_list_key(self):
        summary = source_metadata.summarize([{'resources': [RESPIRATIONS['fhir']]}])
        assert summary['texts'] == ['Respirations']

    def test_nothing_useful_is_empty(self):
        assert source_metadata.summarize([{}, {'text': [None, '  ']}, 'junk']) == {}

    def test_oversized_other_is_dropped(self):
        summary = source_metadata.summarize([
            {'text': ['X'], 'category': {f'k{i}': 'y' * 200 for i in range(20)}},
        ])
        assert summary == {'texts': ['X']}

    def test_curation_import_evidence(self):
        summary = source_metadata.summarize([{
            'records': 40, 'patients': 12, 'codings': 3,
            'category': {'top': 'vital-signs', 'mix': 'vital-signs:100'},
            'value_types': {'quantity': 100.0},
        }])
        assert summary == {'other': {
            'category': {'top': 'vital-signs', 'mix': 'vital-signs:100'},
            'value_types': {'quantity': 100.0},
        }}

    @pytest.mark.parametrize('wrap', [
        lambda r: r, lambda r: {'resource': r}, lambda r: {'fhir': r},
    ])
    def test_nothing_about_the_patient_reaches_the_summary(self, wrap):
        """The summary goes to third-party rankers."""
        resource = {
            **RESPIRATIONS['fhir'],
            'id': 'obs-secret-id',
            'subject': {'reference': 'Patient/secret-patient'},
            'encounter': {'reference': 'Encounter/secret-encounter'},
            'effectiveDateTime': '2026-01-02T03:04:05Z',
            'performer': [{'display': 'Dr Secret'}],
            'note': [{'text': 'secret note'}],
            'identifier': [{'value': 'secret-mrn'}],
        }
        dumped = json.dumps(source_metadata.summarize([wrap(resource)]))
        for secret in ('secret', '2026-01-02', '"16"', ': 16'):
            assert secret not in dumped
        assert 'Respirations' in dumped


def test_context_carries_the_summary_only_when_there_is_one():
    args = dict(source_code='9', vocabulary_id=EPIC, description='Vital signs',
                source_concept=None, umls_name='', domain_id='Measurement',
                omop_table='measurement')
    assert 'source_metadata' not in build_source_context(**args)
    assert build_source_context(**args, source_metadata={'texts': ['Respirations']})[
        'source_metadata'] == {'texts': ['Respirations']}


def _no_text_retrieval(monkeypatch, searched):
    def lexical(text, domain_id, limit=None):
        searched.append(('lexical', text))
        return [{'concept_id': 3024171, 'concept_name': 'Respiratory rate',
                 'concept_code': '9279-1', 'vocabulary_id': 'LOINC',
                 'concept_class_id': 'Clinical Observation', 'domain_id': 'Measurement',
                 'retrieval': 'lexical', 'lexical_score': 0.4}]

    def semantic(text, domain_id, limit=None):
        searched.append(('vectors', text))
        return []

    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lexical)
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', semantic)
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))


def _ranker(monkeypatch, contexts):
    def rank(source_value, candidates, source_description='', **kwargs):
        contexts.append(kwargs['source_context'])
        return candidates[0], 'high confidence: rr', []

    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)


def test_dialog_metadata_reaches_the_ranker_and_retrieval_is_unchanged(loinc, monkeypatch):
    searched, contexts = [], []
    _no_text_retrieval(monkeypatch, searched)
    _ranker(monkeypatch, contexts)
    result = suggest_one_mapping('9', EPIC, 'measurement', source_description='Vital signs',
                                 row_metadata=RESPIRATIONS)

    # Retrieval searches the description only, once per strategy (#1786).
    assert searched == [('lexical', 'Vital signs'), ('vectors', 'Vital signs')]
    assert contexts[0]['original_description'] == 'Vital signs'
    assert contexts[0]['source_metadata']['texts'] == ['Respirations']
    assert {'system': 'http://loinc.org', 'code': '9279-1', 'display': 'Respiratory rate'} \
        in contexts[0]['source_metadata']['codings']
    assert result['strategy_used'] == 'lexical'


def test_no_metadata_no_context_key(loinc, monkeypatch):
    contexts = []
    _no_text_retrieval(monkeypatch, [])
    _ranker(monkeypatch, contexts)
    suggest_one_mapping('9', EPIC, 'measurement', source_description='Vital signs')
    assert 'source_metadata' not in contexts[0]


def test_other_patients_are_never_read(loinc, monkeypatch, django_assert_max_num_queries):
    """Only the row's own metadata: no patient_source_code lookup (#1786)."""
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='9', source_vocabulary_id=EPIC,
        omop_table='measurement', source_metadata=RESPIRATIONS,
    )
    contexts = []
    _no_text_retrieval(monkeypatch, [])
    _ranker(monkeypatch, contexts)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    with CaptureQueriesContext(connection) as queries:
        suggest_one_mapping('9', EPIC, 'measurement', source_description='Vital signs')
    assert not any('patient_source_code' in q['sql'] for q in queries.captured_queries)
    assert 'source_metadata' not in contexts[0]


def test_batch_uses_the_rows_own_metadata(loinc, monkeypatch):
    SourceCodeConceptMapping.objects.create(
        source_code='9', source_vocabulary_id=EPIC, omop_table='measurement',
        domain_id='Measurement', status='proposed', source_code_description='Vital signs',
        occurrence_count=10, source_metadata={**RESPIRATIONS, 'value_types': {'quantity': 100.0}},
    )
    contexts = []
    _no_text_retrieval(monkeypatch, [])
    _ranker(monkeypatch, contexts)
    suggest_mappings('measurement', min_occurrences=1)
    assert contexts[0]['source_metadata']['texts'] == ['Respirations']
    assert contexts[0]['source_metadata']['other'] == {'value_types': {'quantity': 100.0}}


class TestSuggestOneEndpoint:
    @pytest.fixture(autouse=True)
    def _client(self, loinc, monkeypatch):
        from patient_portal.models import Identity
        from rest_framework.test import APIClient

        self.client = APIClient()
        self.client.force_authenticate(Identity.objects.create_user(
            email='meta@test.com', password='pass', is_staff=True))
        self.contexts = []
        _no_text_retrieval(monkeypatch, [])
        _ranker(monkeypatch, self.contexts)

    def _post(self, **extra):
        return self.client.post('/api/v1/code-mappings/suggest-one/', {
            'source_code': '9', 'source_vocabulary_id': EPIC, 'omop_table': 'measurement',
            'source_code_description': 'Vital signs', **extra,
        }, format='json')

    def test_the_dialogs_metadata_is_used(self):
        assert self._post(source_metadata=RESPIRATIONS).status_code == 200
        assert self.contexts[0]['source_metadata']['texts'] == ['Respirations']

    def test_falls_back_to_the_mappings_own(self):
        SourceCodeConceptMapping.objects.create(
            source_code='9', source_vocabulary_id=EPIC, omop_table='measurement',
            status='proposed', source_metadata={'text': ['Respirations']},
        )
        assert self._post().status_code == 200
        assert self.contexts[0]['source_metadata']['texts'] == ['Respirations']

    def test_async_activity_does_not_store_the_metadata(self):
        from omop_core.models import SuggestRun
        from omop_core.services.suggest_jobs import (
            InlineDispatcher, use_dispatcher,
        )
        with use_dispatcher(InlineDispatcher()):
            response = self._post(source_metadata=RESPIRATIONS, **{'async': True})
        assert response.status_code == 202
        run = SuggestRun.objects.get(pk=response.data['run_id'])
        assert 'Patient/' not in str(run.activity) and 'row_metadata' not in str(run.activity)
        assert self.contexts[0]['source_metadata']['texts'] == ['Respirations']


def test_jev_state_names_the_metadata(settings):
    from omop_core.mapping.suggestions import rank_candidates_jev

    settings.JEV_API_KEY = 'test-key'
    response = MagicMock()
    response.json.return_value = {'answers': {'best_match': {
        'choice': '1', 'confidence': 0.9, 'probabilities': {'1': 0.9},
    }}}
    post = MagicMock(return_value=response)
    candidate = {'concept_id': 1, 'concept_name': 'Respiratory rate', 'concept_code': '9279-1',
                 'vocabulary_id': 'LOINC', 'concept_class_id': 'Clinical Observation'}
    context = build_source_context(
        source_code='9', vocabulary_id=EPIC, description='Vital signs', source_concept=None,
        umls_name='', domain_id='Measurement', omop_table='measurement',
        source_metadata=source_metadata.summarize([RESPIRATIONS]),
    )
    with patch('requests.post', post):
        rank_candidates_jev('9', [candidate], 'Vital signs', source_context=context)
    state = post.call_args.kwargs['json']['state']
    assert 'Vital signs' in state
    assert 'Respirations' in state
    assert '9279-1 Respiratory rate' in state
