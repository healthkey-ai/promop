"""Semantic recall, eligibility, fallback isolation and shared ranking."""
from unittest.mock import Mock

import numpy as np
import pytest
from django.db import connection

from omop_core.mapping import suggestions as suggest
from omop_core.models import Concept, ConceptEmbedding, SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory

pytestmark = pytest.mark.django_db


def vector(x=1, y=0):
    return [x, y] + [0.0] * 382


@pytest.fixture
def encoder(monkeypatch):
    model = Mock()
    model.encode.return_value = np.array(vector(), dtype='float32')
    monkeypatch.setattr(suggest, '_get_embedding_model', lambda: model)
    return model


def embedded(name='Semantically related concept', *, embedding=None, **kwargs):
    kwargs.setdefault('standard_concept', 'S')
    kwargs.setdefault('invalid_reason', None)
    concept = ConceptFactory(concept_name=name, **kwargs)
    ConceptEmbedding.objects.create(concept=concept, embedding=embedding or vector())
    return concept


def candidate(cid, retrieval, **scores):
    return dict(concept_id=cid, concept_name=f'Candidate {cid}',
                concept_code=str(cid), vocabulary_id='SNOMED',
                concept_class_id='Clinical Finding', domain_id='Condition',
                retrieval=retrieval, **scores)


def pool(strategies=None, **overrides):
    kwargs = dict(source_code='LOCAL-123', source_vocabulary_id='Local',
                  source_text='informal description', domain_id='Condition',
                  strategies=strategies or suggest.ALL_STRATEGIES)
    kwargs.update(overrides)
    return suggest.retrieval_pool(**kwargs)


def test_cosine_retrieval_finds_concepts_without_lexical_hits(encoder):
    domain = DomainFactory(domain_id='Condition')
    good = embedded('Myocardial infarction', domain=domain)
    embedded('Unrelated spelling', domain=domain, embedding=vector(0, 1))
    assert suggest.lexical_candidates('heart attack', 'Condition') == []
    hits = suggest.semantic_candidates('heart attack', 'Condition', limit=1)
    assert [hit['concept_id'] for hit in hits] == [good.pk]
    assert hits[0]['semantic_score'] == 1.0
    assert hits[0]['vector_distance'] == 0.0
    assert hits[0]['retrieval'] == 'semantic'
    assert 'vector_score' not in hits[0]
    encoder.encode.assert_called_once_with('heart attack')


def test_filters_before_limit_and_restores_timeout(encoder):
    domain = DomainFactory(domain_id='Condition')
    embedded(domain=domain, standard_concept=None)
    embedded(domain=domain, invalid_reason='D')
    embedded(domain=DomainFactory(domain_id='Drug'))
    good = embedded(domain=domain, embedding=vector(0.8, 0.2))
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        before = cursor.fetchone()[0]
    hits = suggest.semantic_candidates('heart attack', 'Condition', limit=1)
    assert [hit['concept_id'] for hit in hits] == [good.pk]
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        assert cursor.fetchone()[0] == before


def test_empty_corpus_does_not_load_model(encoder):
    assert suggest.semantic_candidates('heart attack', 'Condition') == []
    encoder.encode.assert_not_called()


@pytest.mark.parametrize('query', ['', 'a', 'ab', '  '])
def test_short_input_does_not_load_model(query, encoder):
    assert suggest.semantic_candidates(query, 'Condition') == []
    encoder.encode.assert_not_called()


def test_encoder_failure_preserves_database(encoder):
    embedded()
    encoder.encode.side_effect = RuntimeError('model unavailable')
    assert suggest.semantic_candidates('heart attack', None) == []
    assert ConceptEmbedding.objects.count() == 1


def test_database_timeout_rolls_back_only_semantic_query(encoder, settings):
    embedded()
    settings.SUGGEST_SEMANTIC_TIMEOUT_MS = 1
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        before = cursor.fetchone()[0]

    def slow_query(execute, sql, params, many, context):
        if '<=>' in sql:
            execute('SELECT pg_sleep(0.05)', None, False, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(slow_query):
        assert suggest.semantic_candidates('heart attack', None) == []
    # The caller's transaction and earlier writes survive the cancelled query.
    assert ConceptEmbedding.objects.count() == 1
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        assert cursor.fetchone()[0] == before


def test_missing_embedding_table_does_not_poison_transaction(encoder):
    def missing_table(execute, sql, params, many, context):
        if 'concept_embedding' in sql:
            return execute('SELECT * FROM missing_semantic_test_table', None, False, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(missing_table):
        assert suggest.semantic_candidates('heart attack', None) == []
    assert Concept.objects.count() >= 0
    encoder.encode.assert_not_called()


@pytest.mark.parametrize('lexical_hits', [[], [candidate(1, 'lexical', lexical_score=0.6)]])
def test_umls_miss_runs_both_retrievers_and_ranks_once(monkeypatch, lexical_hits):
    lexical = Mock(return_value=lexical_hits)
    semantic = Mock(return_value=[candidate(2, 'semantic', semantic_score=0.9)])
    ranker = Mock(side_effect=lambda source, candidates, **kw: (candidates[-1], 'chosen'))
    monkeypatch.setattr(suggest, 'umls_candidates', Mock(return_value=([], None)))
    monkeypatch.setattr(suggest, 'lexical_candidates', lexical)
    monkeypatch.setattr(suggest, 'semantic_candidates', semantic)
    monkeypatch.setattr(suggest, 'rank_candidates', ranker)
    result = suggest.suggest_one_mapping('LOCAL-123', 'Local', 'condition',
                                        source_description='informal description',
                                        strategies=['umls', 'lexical', 'semantic'])
    lexical.assert_called_once()
    semantic.assert_called_once()
    ranker.assert_called_once()
    assert result['suggested']['concept_id'] == 2
    assert result['strategy_used'] == 'semantic'
    assert result['candidates_considered'] == len(lexical_hits) + 1
    assert result['vector_reranked'] is False


def test_single_umls_hit_continues_and_reports_each_stage(monkeypatch):
    hit = candidate(1, 'umls', umls_score=1.0)
    monkeypatch.setattr(suggest, 'umls_candidates', Mock(return_value=([hit], 'C123')))
    lexical = Mock(return_value=[candidate(2, "lexical")])
    semantic = Mock(return_value=[candidate(3, "semantic", semantic_score=0.8, vector_distance=0.2)])
    monkeypatch.setattr(suggest, 'lexical_candidates', lexical)
    monkeypatch.setattr(suggest, 'semantic_candidates', semantic)
    events = []
    def received(strategy, hits):
        if strategy == 'umls':
            lexical.assert_not_called()
            semantic.assert_not_called()
        elif strategy == 'lexical':
            semantic.assert_not_called()
        events.append((strategy, hits))
    hits, cui, definitive = pool(strategies=['umls', 'lexical', 'semantic'], on_candidates=received)
    assert [c['concept_id'] for c in hits] == [1, 2, 3]
    assert cui == 'C123'
    assert definitive
    assert [stage for stage, _ in events] == ['umls', 'lexical', 'semantic']
    assert events[-1][1][0]['vector_distance'] == 0.2
    lexical.assert_called_once()
    semantic.assert_called_once()


def test_deduplicates_and_does_not_rerank_semantic_candidates(monkeypatch):
    monkeypatch.setattr(suggest, 'umls_candidates', Mock(return_value=(
        [candidate(1, 'umls'), candidate(2, 'umls')], 'C123')))
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(return_value=[candidate(3, 'lexical')]))
    monkeypatch.setattr(suggest, 'semantic_candidates', Mock(return_value=[
        candidate(1, 'semantic', semantic_score=0.8),
        candidate(3, 'semantic', semantic_score=0.7),
        candidate(4, 'semantic', semantic_score=0.6),
    ]))
    rerank = Mock(side_effect=lambda text, hits: (hits, False))
    monkeypatch.setattr(suggest, 'vector_rerank', rerank)
    hits, _, definitive = pool()
    assert [hit['concept_id'] for hit in hits] == [1, 2, 3, 4]
    assert [hit['retrieval'] for hit in hits] == ['umls', 'umls', 'lexical', 'semantic']
    assert hits[0]['semantic_score'] == 0.8
    assert hits[2]['semantic_score'] == 0.7
    assert not definitive
    assert [[c['concept_id'] for c in call.args[1]] for call in rerank.call_args_list] == [[1, 2], [3]]


def test_disabled_semantic_does_no_work(monkeypatch):
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(return_value=[]))
    semantic = Mock()
    monkeypatch.setattr(suggest, 'semantic_candidates', semantic)
    assert pool(['lexical'])[0] == []
    semantic.assert_not_called()


def test_default_pipeline_sends_full_pool_to_llm_without_vector_reranking(monkeypatch):
    lexical_hit = candidate(1, 'lexical', lexical_score=0.5)
    semantic_hit = candidate(2, 'semantic', semantic_score=0.9)
    monkeypatch.setattr(suggest, 'umls_candidates', Mock(return_value=([], None)))
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(return_value=[lexical_hit]))
    monkeypatch.setattr(suggest, 'semantic_candidates', Mock(return_value=[semantic_hit]))
    reranker = Mock()
    ranker = Mock(return_value=(semantic_hit, 'chosen'))
    monkeypatch.setattr(suggest, 'vector_rerank', reranker)
    monkeypatch.setattr(suggest, 'rank_candidates', ranker)
    result = suggest.suggest_one_mapping('LOCAL-123', 'Local', 'condition')
    reranker.assert_not_called()
    assert [c['concept_id'] for c in ranker.call_args.args[1]] == [1, 2]
    assert result['strategy_used'] == 'semantic'


def test_icd_semantic_can_retrieve_other_domains(encoder):
    good = embedded(domain=DomainFactory(domain_id='Observation'))
    hits, _, _ = pool(['semantic'], source_vocabulary_id='ICD10CM')
    assert [hit['concept_id'] for hit in hits] == [good.pk]


def test_semantic_provenance_is_saved_on_proposed_mapping(encoder, settings):
    settings.ANTHROPIC_API_KEY = ''
    good = embedded(domain=DomainFactory(domain_id='Condition'))
    mapping = SourceCodeConceptMapping.objects.create(
        source_code='LOCAL-123', source_vocabulary_id='Local', domain_id='Condition',
        source_code_description='heart attack', omop_table='condition',
        status='proposed', occurrence_count=12,
    )
    suggest.suggest_mappings('condition', strategies=['semantic'])
    mapping.refresh_from_db()
    assert mapping.target_concept_id == good.pk
    assert mapping.suggest_strategy == 'semantic'
    assert mapping.status == 'proposed'
    assert mapping.suggestion_model_version == suggest.SUGGESTION_MODEL_VERSION
