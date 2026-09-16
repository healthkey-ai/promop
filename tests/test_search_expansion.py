"""One bounded rewrite can recover retrieval misses, but cannot approve its own hypothesis."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import connection

from omop_core.mapping import suggestions as suggest
from omop_core.mapping.search_expansion import generate_search_query
from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db


def candidate(concept):
    return dict(concept_id=concept.pk, concept_name=concept.concept_name,
                concept_code=concept.concept_code, vocabulary_id=concept.vocabulary_id,
                domain_id=concept.domain_id, concept_class_id=concept.concept_class_id,
                retrieval='lexical', lexical_score=0.8)


@pytest.fixture
def llm(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = 'test-expansion-key'
    client = Mock()
    monkeypatch.setattr('anthropic.Anthropic', lambda **kwargs: client)
    return client


def reply(value):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(value))])


def select(cid=None):
    return reply(dict(concept_id=cid, confidence='high' if cid else 'low', reason='Compared to original evidence.'))


@pytest.mark.parametrize('initial_hit', [False, True])
def test_rewrite_recovers_missing_candidate_and_keeps_original_source(initial_hit, monkeypatch, llm):
    wrong, right = ConceptFactory(), ConceptFactory()
    retrieved = Mock(side_effect=[[candidate(wrong)] if initial_hit else [], [candidate(right)]])
    monkeypatch.setattr(suggest, 'lexical_candidates', retrieved)
    responses = ([select()] if initial_hit else []) + [reply({'search_query': 'formal assay name'}), select(right.pk)]
    llm.messages.create.side_effect = responses
    result = suggest.suggest_one_mapping('LOCAL', 'Local lab', 'measurement',
        source_description='Original ambiguous assay', strategies=['lexical'])
    assert result['suggested']['concept_id'] == right.pk
    assert result['query_expansion'] == 'formal assay name'
    assert 'Search expanded' in result['note']
    final = json.loads(llm.messages.create.call_args.kwargs['messages'][0]['content'])
    assert final['source']['original_description'] == 'Original ambiguous assay'
    assert final['source']['code'] == 'LOCAL'
    assert [c['concept_id'] for c in final['candidates']] == ([wrong.pk] if initial_hit else []) + [right.pk]
    assert final['candidates'][-1]['generated_search_query'] == 'formal assay name'
    assert retrieved.call_args.args[0] == 'formal assay name'
    assert retrieved.call_args.args[1] == 'Measurement'
    assert llm.messages.create.call_count == (3 if initial_hit else 2)


def test_no_shortcut_for_close_cosine_score(monkeypatch, llm):
    target = candidate(ConceptFactory()) | {'retrieval': 'semantic', 'semantic_score': 0.9999}
    monkeypatch.setattr(suggest, 'semantic_candidates', Mock(return_value=[target]))
    llm.messages.create.return_value = select(target['concept_id'])
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['semantic'])
    assert result['suggested']['concept_id'] == target['concept_id']
    assert result['query_expansion'] is None
    assert llm.messages.create.call_count == 1


def test_retry_abstention_stops_without_another_rewrite(monkeypatch, llm):
    target = ConceptFactory()
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(side_effect=[[], [candidate(target)]]))
    llm.messages.create.side_effect = [reply({'search_query': 'formal assay name'}), select()]
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['lexical'])
    assert result['suggested'] is None
    assert llm.messages.create.call_count == 2


def test_retry_model_failure_does_not_promote_retrieved_candidate(monkeypatch, llm):
    target = ConceptFactory()
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(side_effect=[[], [candidate(target)]]))
    llm.messages.create.side_effect = [reply({'search_query': 'formal assay name'}), RuntimeError('unavailable')]
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['lexical'])
    assert result['suggested'] is None
    assert 'Ranking model unavailable' in result['note']


def test_unsupported_id_after_rewrite_is_rejected(monkeypatch, llm):
    target = ConceptFactory()
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(side_effect=[[], [candidate(target)]]))
    llm.messages.create.side_effect = [reply({'search_query': 'formal assay name'}), select(123456)]
    assert suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['lexical'])['suggested'] is None


def test_duplicate_only_retry_does_not_rerank_or_loop(monkeypatch, llm):
    target = candidate(ConceptFactory())
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(return_value=[target]))
    llm.messages.create.side_effect = [select(), reply({'search_query': 'formal assay name'})]
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['lexical'])
    assert result['suggested'] is None
    assert result['candidates_considered'] == 1
    assert 'no new candidates found' in result['note']
    assert llm.messages.create.call_count == 2


def test_retry_database_failure_preserves_original_result_and_transaction(monkeypatch, llm):
    target = ConceptFactory()

    def retrieve(query, *args, **kwargs):
        if query == 'formal assay name':
            with connection.cursor() as cursor:
                cursor.execute('SELECT * FROM missing_expansion_test_table')
        return [candidate(target)]

    monkeypatch.setattr(suggest, 'lexical_candidates', retrieve)
    llm.messages.create.side_effect = [select(), reply({'search_query': 'formal assay name'})]
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['lexical'])
    assert result['suggested'] is None
    assert 'was unavailable' in result['note']
    assert result['candidates_considered'] == 1
    assert type(target).objects.filter(pk=target.pk).exists()


@pytest.mark.parametrize('query', ['LOCAL', 'original assay'])
def test_unchanged_query_does_not_repeat_retrieval(query, monkeypatch, llm):
    retrieved = Mock(return_value=[])
    monkeypatch.setattr(suggest, 'lexical_candidates', retrieved)
    llm.messages.create.return_value = reply({'search_query': query})
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement',
        source_description='Original assay', strategies=['lexical'])
    assert result['suggested'] is None
    assert result['query_expansion'] is None
    retrieved.assert_called_once()


def test_umls_only_does_not_enable_disabled_text_retrievers(monkeypatch, llm):
    lexical, semantic = Mock(), Mock()
    monkeypatch.setattr(suggest, 'lexical_candidates', lexical)
    monkeypatch.setattr(suggest, 'semantic_candidates', semantic)
    result = suggest.suggest_one_mapping('LOCAL', '', 'measurement', strategies=['umls'])
    assert result['suggested'] is None
    lexical.assert_not_called()
    semantic.assert_not_called()
    llm.messages.create.assert_not_called()


def test_batch_saves_retry_provenance_without_replacing_original_description(monkeypatch, llm):
    target = ConceptFactory()
    mapping = SourceCodeConceptMapping.objects.create(source_code='LOCAL', source_vocabulary_id='',
        source_code_description='Original assay', domain_id='Measurement', omop_table='measurement',
        status='proposed', occurrence_count=12)
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(side_effect=[[], [candidate(target)]]))
    llm.messages.create.side_effect = [reply({'search_query': 'formal assay name'}), select(target.pk)]
    result = suggest.suggest_mappings('measurement', strategies=['lexical'])[0]
    mapping.refresh_from_db()
    assert mapping.target_concept_id == target.pk
    assert mapping.status == 'proposed'
    assert mapping.source_code_description == 'Original assay'
    assert 'formal assay name' in mapping.notes
    assert result['query_expansion'] == 'formal assay name'


@pytest.mark.parametrize('query', [None, '', ' ', 42, 'x' * 256])
def test_invalid_generated_queries_are_ignored(query, llm):
    llm.messages.create.return_value = reply({'search_query': query})
    assert generate_search_query({'original_description': 'assay'}, [], '') is None
