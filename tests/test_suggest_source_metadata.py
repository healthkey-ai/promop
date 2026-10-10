"""Suggest reads source_metadata, not only the description (#1782).

The motivating case: Epic adds LOINC 8716-3 "Vital signs" to every vital sign,
so a respirations Observation reaches Suggest described as "Vital signs". Its
metadata says "Respirations" and carries LOINC 9279-1 "Respiratory rate".
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from omop_core.mapping import source_metadata
from omop_core.mapping.suggestion_context import build_source_context, candidate_context
from omop_core.models import (
    ConceptRelationship, PatientSourceCode, Relationship, SourceCodeConceptMapping,
)
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


class TestSearchTexts:
    def test_the_one_name(self):
        summary = source_metadata.summarize([RESPIRATIONS])
        assert source_metadata.search_texts(
            summary, description='Vital signs', source_code='9',
        ) == ['Respirations']

    def test_several_names_search_none(self):
        """Searching one of several would steer the pool toward it."""
        assert source_metadata.search_texts({'texts': ['Blood Pressure', 'Respirations']}) == []

    def test_already_the_description(self):
        assert source_metadata.search_texts(
            {'texts': ['Respirations']}, description='respirations') == []


class TestGather:
    def test_mapping_then_patient_rows(self):
        person_a, person_b = PersonFactory(), PersonFactory()
        for person, text in ((person_a, 'Respirations'), (person_b, 'Respirations')):
            PatientSourceCode.objects.create(
                person=person, source_value='9', source_vocabulary_id=EPIC,
                omop_table='measurement', source_metadata={'text': [text]},
            )
        other = PersonFactory()
        PatientSourceCode.objects.create(
            person=other, source_value='9', source_vocabulary_id='OTHER',
            omop_table='measurement', source_metadata={'text': ['Something else']},
        )
        found = source_metadata.gather(
            source_code='9', source_vocabulary_id=EPIC, omop_table='measurement',
            mapping_metadata={'patients': 12},
        )
        # Identical patient rows collapse; another vocabulary's code "9" is not this one.
        assert found == [{'patients': 12}, {'text': ['Respirations']}]

    def test_at_most_five_patient_rows(self):
        for i in range(8):
            PatientSourceCode.objects.create(
                person=PersonFactory(), source_value='9', source_vocabulary_id=EPIC,
                omop_table='measurement', source_metadata={'text': [f'T{i}']},
            )
        found = source_metadata.gather(
            source_code='9', source_vocabulary_id=EPIC, omop_table='measurement')
        assert len(found) == source_metadata.MAX_METADATA_ROWS

    def test_table_aliases(self):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='I10', source_vocabulary_id='ICD10CM',
            omop_table='condition_occurrence', source_metadata={'text': ['Hypertension']},
        )
        assert source_metadata.gather(
            source_code='I10', source_vocabulary_id='ICD10CM', omop_table='condition',
        ) == [{'text': ['Hypertension']}]


class TestCodedCandidates:
    def test_standard_coding_is_a_candidate(self, loinc):
        summary = source_metadata.summarize([RESPIRATIONS])
        hits = source_metadata.coded_candidates(
            summary, 'Measurement', source_vocabulary_id=EPIC, source_code='9',
        )
        by_id = {hit['concept_id']: hit for hit in hits}
        # Both LOINC codings on the resource; the Epic and Finnish OIDs are not
        # vocabularies we hold.
        assert set(by_id) == {3024171, 3036277}
        assert by_id[3024171]['retrieval'] == 'metadata'
        assert by_id[3024171]['metadata_coding']['display'] == 'Respiratory rate'
        assert candidate_context(by_id[3024171])['source_metadata_coding']['code'] == '9279-1'

    def test_own_code_is_skipped(self, loinc):
        summary = source_metadata.summarize([RESPIRATIONS])
        hits = source_metadata.coded_candidates(
            summary, 'Measurement', source_vocabulary_id='LOINC', source_code='8716-3',
        )
        assert [hit['concept_id'] for hit in hits] == [3024171]

    def test_domain_is_respected(self, loinc):
        summary = source_metadata.summarize([RESPIRATIONS])
        assert source_metadata.coded_candidates(summary, 'Condition') == []

    def test_nonstandard_code_follows_maps_to(self, loinc):
        retired = ConceptFactory(
            concept_id=40000001, concept_name='Old respirations', concept_code='OLD-1',
            vocabulary=loinc['vocabulary'], domain=loinc['domain'],
            concept_class=loinc['class'], standard_concept='',
        )
        maps_to = Relationship.objects.create(
            relationship_id='Maps to', relationship_name='Maps to', is_hierarchical=0,
            defines_ancestry=0, reverse_relationship_id='Mapped from', relationship_concept_id=0,
        )
        ConceptRelationship.objects.create(
            concept_1=retired, concept_2=loinc['respiratory_rate'], relationship=maps_to,
            valid_start_date='2000-01-01', valid_end_date='2099-12-31',
        )
        summary = {'codings': [{'system': 'http://loinc.org', 'code': 'OLD-1', 'display': ''}]}
        hits = source_metadata.coded_candidates(summary, 'Measurement')
        assert [hit['concept_id'] for hit in hits] == [3024171]
        assert hits[0]['metadata_coding']['code'] == 'OLD-1'


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
        return []

    def semantic(text, domain_id, limit=None):
        searched.append(('vectors', text))
        return []

    monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates', lexical)
    monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', semantic)
    monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))


def test_dialog_suggest_uses_patient_metadata(loinc, monkeypatch):
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='9', source_vocabulary_id=EPIC,
        omop_table='measurement', source_metadata=RESPIRATIONS,
    )
    searched, ranked = [], {}
    _no_text_retrieval(monkeypatch, searched)

    def rank(source_value, candidates, source_description='', **kwargs):
        ranked.update(candidates=candidates, context=kwargs['source_context'])
        return next(c for c in candidates if c['concept_id'] == 3024171), 'high confidence: rr', []

    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)
    result = suggest_one_mapping('9', EPIC, 'measurement', source_description='Vital signs')

    assert ('lexical', 'Respirations') in searched
    assert ('vectors', 'Respirations') in searched
    assert ranked['context']['original_description'] == 'Vital signs'
    assert ranked['context']['source_metadata']['texts'] == ['Respirations']
    assert result['suggested']['concept_id'] == 3024171
    assert result['strategy_used'] == 'metadata'


def test_batch_suggest_uses_queue_and_patient_metadata(loinc, monkeypatch):
    mapping = SourceCodeConceptMapping.objects.create(
        source_code='9', source_vocabulary_id=EPIC, omop_table='measurement',
        domain_id='Measurement', status='proposed', source_code_description='Vital signs',
        occurrence_count=10, source_metadata={'patients': 3, 'value_types': {'quantity': 100.0}},
    )
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='9', source_vocabulary_id=EPIC,
        omop_table='measurement', source_metadata=RESPIRATIONS,
    )
    searched, contexts = [], []
    _no_text_retrieval(monkeypatch, searched)

    def rank(source_value, candidates, source_description='', **kwargs):
        contexts.append(kwargs['source_context'])
        return next(c for c in candidates if c['concept_id'] == 3024171), 'high confidence: rr', []

    monkeypatch.setattr('omop_core.mapping.suggestions.rank_candidates', rank)
    suggest_mappings('measurement', min_occurrences=1)

    assert contexts[0]['source_metadata']['other'] == {'value_types': {'quantity': 100.0}}
    assert contexts[0]['source_metadata']['texts'] == ['Respirations']
    mapping.refresh_from_db()
    assert mapping.target_concept_id == 3024171
    assert mapping.suggest_strategy == 'metadata'


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


class TestPoolOrder:
    def _pool(self, monkeypatch, strategies, lexical_hits=()):
        from omop_core.mapping.suggestions import retrieval_pool

        monkeypatch.setattr('omop_core.mapping.suggestions.lexical_candidates',
                            lambda *a, **k: [dict(h) for h in lexical_hits])
        monkeypatch.setattr('omop_core.mapping.suggestions.semantic_candidates', lambda *a, **k: [])
        monkeypatch.setattr('omop_core.mapping.suggestions.umls_candidates', lambda *a: ([], None))
        candidates, _, _ = retrieval_pool(
            source_code='9', source_vocabulary_id=EPIC, source_text='Vital signs',
            domain_id='Measurement', strategies=strategies,
            metadata_summary=source_metadata.summarize([RESPIRATIONS]),
        )
        return candidates

    def test_codings_go_last(self, loinc, monkeypatch):
        """A ranker outage falls back to candidates[0]; it must not be a coding."""
        lexical = {'concept_id': 1, 'concept_name': 'Vital signs panel', 'concept_code': 'X',
                   'vocabulary_id': 'LOINC', 'concept_class_id': 'Panel',
                   'retrieval': 'lexical', 'lexical_score': 0.9}
        pool = self._pool(monkeypatch, ['umls', 'lexical'], [lexical])
        assert pool[0]['concept_id'] == 1
        assert {c['concept_id'] for c in pool[1:]} == {3024171, 3036277}

    def test_a_coding_already_in_the_pool_is_annotated(self, loinc, monkeypatch):
        lexical = {'concept_id': 3024171, 'concept_name': 'Respiratory rate',
                   'concept_code': '9279-1', 'vocabulary_id': 'LOINC',
                   'concept_class_id': 'Clinical Observation', 'retrieval': 'lexical'}
        pool = self._pool(monkeypatch, ['umls', 'lexical'], [lexical])
        rr = [c for c in pool if c['concept_id'] == 3024171]
        assert len(rr) == 1
        assert rr[0]['retrieval'] == 'lexical'
        assert rr[0]['metadata_coding']['code'] == '9279-1'

    def test_codings_need_umls_enabled(self, loinc, monkeypatch):
        assert self._pool(monkeypatch, ['lexical', 'vectors']) == []
