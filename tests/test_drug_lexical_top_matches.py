"""Exact before/after comparisons against the implementation at 8798ec24."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from django.db import DatabaseError, connection, transaction
from django.test.utils import CaptureQueriesContext

from omop_core.mapping import suggestions
from omop_core.models import ConceptSynonym
from omop_core.services import suggest_synonym_terms
from tests.factories import ConceptFactory, DomainFactory
from tests.lexical_reference import lexical_candidates as before

pytestmark = pytest.mark.django_db


@pytest.fixture
def corpus():
    drug = DomainFactory(domain_id='Drug')
    names = [
        'aspirin 81 MG Oral Tablet', 'aspirin 325 MG Oral Tablet',
        'warfarin sodium 5 MG Oral Tablet', 'acetylsalicylic acid 81 MG Oral Tablet',
        'metformin hydrochloride 500 MG Extended Release Tablet',
        'metformin hydrochloride 1000 MG Oral Tablet',
        'insulin glargine 100 UNT/ML Injectable Solution',
        'Salicylic Acid 0.5 MG/MG Topical Ointment',
        'Norfloxacin-containing product', 'Famciclovir-containing product',
        'amoxicillin 500 MG / clavulanate 125 MG Oral Tablet',
        'Granisetron hydrochloride 0.2 MG Oral Tablet',
        'Vitamin B12 1000 MCG Oral Tablet', 'Sodium Chloride 0.9% Injection',
    ]
    # Ties straddle LIMIT; multiple synonyms must count as ONE concept.
    names += ['aspirin 81 MG Oral Tablet'] * 12
    names += [f'aspirin {dose} MG Oral Tablet' for dose in range(70, 100)]
    concepts = [ConceptFactory(concept_name=name, domain=drug) for name in names]
    language = ConceptFactory()
    for concept in concepts[::2]:
        for name in (concept.concept_name, concept.concept_name.upper(),
                     concept.concept_name + ' [brand]'):
            ConceptSynonym.objects.create(concept=concept, concept_synonym_name=name,
                                         language_concept_id=language.pk)
    for domain, kwargs in [('Drug', {'invalid_reason': 'D'}),
                           ('Drug', {'standard_concept': None}), ('Procedure', {})]:
        ConceptFactory(concept_name=names[0], domain=DomainFactory(domain_id=domain), **kwargs)
    return concepts


QUERIES = [
    'ASPIRIN 81 MG ORAL TABLET', '  aspirin 81 mg oral tablet  ',
    'ASPIRIN TABLET', 'METFORMIN HYDROCHLORIDE 500 MG EXTENDED RELEASE TABLET',
    'Salicylic acid 500 mg/g cutaneous unguent', 'Norfloxacin-containing product',
    'Famciclovir-containing product', 'Insulin Glargine 100 UNT/ML Injectable Solution',
    'amoxicillin 500 MG / clavulanate 125 MG Oral Tablet',
    'Granisetron (as granisetron hydrochloride) 200 micrograms',
    'Vitamin B12 1000 MCG Oral Tablet', 'Sodium Chloride 0.9% Injection',
    'asprin 81 MG Oral Tablet', 'unfindablexyz', 'MG ORAL TABLET', '', None, 'a',
]


@pytest.mark.parametrize('cached_synonyms', [False, True])
@pytest.mark.parametrize('limit', [1, 3, 10, 100])
def test_full_candidate_output_matches_frozen_baseline(corpus, cached_synonyms, limit):
    if cached_synonyms:
        suggest_synonym_terms.refresh()
        # A stale derived term must retain the old eligibility check.
        corpus[0].invalid_reason = 'D'
        corpus[0].save(update_fields=['invalid_reason'])
    for query in QUERIES:
        assert suggestions.lexical_candidates(query, 'Drug', limit) == before(query, 'Drug', limit), query


@pytest.mark.parametrize('domain', ['Condition', 'Procedure', 'Measurement', None])
def test_other_domains_keep_results_and_query_count(corpus, domain):
    with CaptureQueriesContext(connection) as baseline_queries:
        expected = before('ASPIRIN 81 MG ORAL TABLET', domain)
    with CaptureQueriesContext(connection) as actual_queries:
        assert suggestions.lexical_candidates('ASPIRIN 81 MG ORAL TABLET', domain) == expected
    assert len(actual_queries) == len(baseline_queries)


@pytest.mark.parametrize('query', ['ASPIRIN', 'unfindablexyz'])
def test_single_words_keep_the_original_query_count(corpus, query):
    with CaptureQueriesContext(connection) as baseline_queries:
        expected = before(query, 'Drug')
    with CaptureQueriesContext(connection) as actual_queries:
        assert suggestions.lexical_candidates(query, 'Drug') == expected
    assert len(actual_queries) == len(baseline_queries)


def _threshold():
    with connection.cursor() as cursor:
        cursor.execute('SHOW pg_trgm.similarity_threshold')
        return cursor.fetchone()[0]


@pytest.mark.parametrize('threshold', ['0.1', '0.3', '0.6', '0.8'])
def test_preserves_session_threshold_and_results(corpus, threshold):
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('pg_trgm.similarity_threshold', %s, true)", [threshold])
        for query in QUERIES[:5]:
            expected = before(query, 'Drug', 3)
            assert suggestions.lexical_candidates(query, 'Drug', 3) == expected
            assert float(_threshold()) == float(threshold)


def test_sparse_probe_falls_back_even_when_not_empty(corpus):
    query = 'INSULIN GLARGINE 100 UNT/ML INJECTABLE SOLUTION'
    with CaptureQueriesContext(connection) as queries:
        actual = suggestions.lexical_candidates(query, 'Drug', 10)
    assert actual == before(query, 'Drug', 10)
    assert actual
    # Two name queries: a nonempty but incomplete probe must NOT truncate recall.
    name_queries = [q['sql'] for q in queries if 'UPPER("concept"."concept_name")' in q['sql']]
    assert len(name_queries) == 2


def test_complete_probe_does_not_repeat_name_search(corpus):
    query = 'ASPIRIN 81 MG ORAL TABLET'
    with CaptureQueriesContext(connection) as queries:
        actual = suggestions.lexical_candidates(query, 'Drug', 3)
    assert actual == before(query, 'Drug', 3)
    name_queries = [q['sql'] for q in queries if 'UPPER("concept"."concept_name")' in q['sql']]
    assert len(name_queries) == 1


def test_threshold_restored_after_database_error(corpus):
    previous = _threshold()
    # Fail after SET LOCAL, while evaluating the name query.
    from django.db.models.query import QuerySet
    original = QuerySet._fetch_all

    def fail_names(qs):
        if qs.model is suggestions.Concept and 'score' in qs.query.annotations:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 / 0')
        return original(qs)

    with patch.object(QuerySet, '_fetch_all', fail_names), pytest.raises(DatabaseError):
        suggestions.lexical_candidates('ASPIRIN 81 MG ORAL TABLET', 'Drug')
    assert _threshold() == previous
    assert before('ASPIRIN 81 MG ORAL TABLET', 'Drug')


@pytest.mark.parametrize('cached_synonyms', [False, True])
def test_cutoff_is_inclusive_and_ties_keep_the_same_ids(cached_synonyms):
    concepts = [ConceptFactory(concept_name='a b c d e', domain=DomainFactory(domain_id='Drug'))
                for _ in range(12)]
    language = ConceptFactory()
    for concept in concepts:
        ConceptSynonym.objects.create(concept=concept, concept_synonym_name='a b c d e',
                                     language_concept_id=language.pk)
    if cached_synonyms:
        suggest_synonym_terms.refresh()
    with connection.cursor() as cursor:
        cursor.execute("SELECT similarity('A B C', 'A B C D E')")
        assert cursor.fetchone()[0] == pytest.approx(0.6)
    actual = suggestions.lexical_candidates('a b c', 'Drug', 10)
    assert actual == before('a b c', 'Drug', 10)
    assert [row['concept_id'] for row in actual] == sorted(c.pk for c in concepts)[:10]


def test_ranker_gets_identical_evidence_and_preserves_recorded_decision(corpus, settings):
    """Replay a fixed provider response; live model sampling is not an equality oracle."""
    settings.ANTHROPIC_API_KEY = 'test-only'
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
        type='text', text=json.dumps({'concept_id': corpus[0].pk, 'confidence': 'high',
                                     'reason': 'Exact product match.'}),
    )])
    kwargs = dict(source_code='test-aspirin', source_vocabulary_id='RxNorm',
                  source_text='ASPIRIN 81 MG ORAL TABLET', domain_id='Drug',
                  strategies=['lexical'], lexical_limit=10)
    with patch.object(suggestions, 'lexical_candidates', before):
        old_job = suggestions._prepare(**kwargs)
    new_job = suggestions._prepare(**kwargs)
    assert new_job == old_job
    outputs = []
    with patch('anthropic.Anthropic', return_value=client):
        for job in (old_job, new_job):
            outputs.append(suggestions.rank_candidates(
                job['source_code'], job['candidates'], job['source_text'],
                source_context=job['source_context'],
            ))
    assert outputs[0] == outputs[1]
    assert outputs[1][0]['concept_id'] == corpus[0].pk
    assert client.messages.create.call_args_list[0] == client.messages.create.call_args_list[1]
