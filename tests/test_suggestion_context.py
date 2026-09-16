"""Evidence survives retrieval and reaches the LLM without extra worker DB reads."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.db import connection

from omop_core.mapping import suggestions as suggest
from omop_core.mapping.suggestion_context import (
    MAX_EVIDENCE_ITEMS, build_source_context, enrich_candidates,
)
from omop_core.models import (
    ConceptRelationship, ConceptSynonym, Relationship, SourceCodeConceptMapping,
    UmlsConcept, UmlsRelease, UmlsSourceCode,
)
from tests.factories import ConceptFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def candidate(concept):
    return {
        'concept_id': concept.pk, 'concept_name': concept.concept_name,
        'concept_code': concept.concept_code, 'vocabulary_id': concept.vocabulary_id,
        'domain_id': concept.domain_id, 'concept_class_id': concept.concept_class_id,
        'retrieval': 'lexical', 'lexical_score': 0.8,
    }


def synonym(concept, text):
    ConceptFactory(concept_id=4180186, concept_code='LANG-EN', concept_name='English')
    ConceptSynonym.objects.create(concept=concept, concept_synonym_name=text,
                                 language_concept_id=4180186)


def relation(source, target, name='Maps to', **kwargs):
    rel, _ = Relationship.objects.get_or_create(relationship_id=name, defaults={
        'relationship_name': name, 'is_hierarchical': 0, 'defines_ancestry': 0,
        'reverse_relationship_id': name, 'relationship_concept_id': 0,
    })
    values = dict(concept_1=source, concept_2=target, relationship=rel,
                  valid_start_date='1970-01-01', valid_end_date='2099-12-31', invalid_reason=None)
    return ConceptRelationship.objects.create(**(values | kwargs))


@pytest.fixture
def llm(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = 'test-context-key'
    client = Mock()
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
        type='text', text=json.dumps({'concept_id': None, 'confidence': 'low',
                                     'reason': 'Conflicting source labels: needs review.'}),
    )])
    monkeypatch.setattr('anthropic.Anthropic', lambda **kwargs: client)
    return client


def payload(llm):
    return next(data for call in reversed(llm.messages.create.call_args_list)
                if 'candidates' in (data := json.loads(call.kwargs['messages'][0]['content'])))


def test_synonym_that_retrieved_candidate_is_preserved():
    concept = ConceptFactory(concept_name='Unrelated preferred wording')
    synonym(concept, 'Distinctive source assay')
    hits = suggest.lexical_candidates('Distinctive source assay', 'Measurement')
    assert [c['concept_id'] for c in hits] == [concept.pk]
    enriched = enrich_candidates(hits, 'Distinctive source assay', None, min_similarity=0.3)
    assert enriched[0]['matched_synonyms'] == [
        {'text': 'Distinctive source assay', 'language_concept_id': 4180186},
    ]


def test_synonym_context_is_bounded_per_candidate_and_preserves_pool_order():
    first, second = ConceptFactory(), ConceptFactory()
    for i in range(8):
        synonym(first, f'Distinctive source assay {i}')
    synonym(second, 'Distinctive source assay')
    synonym(second, 'unrelated wording')
    hits = enrich_candidates([candidate(second), candidate(first)], 'Distinctive source assay',
                             None, min_similarity=0.3)
    assert [c['concept_id'] for c in hits] == [second.pk, first.pk]
    assert len(hits[0]['matched_synonyms']) == 1
    assert len(hits[1]['matched_synonyms']) == MAX_EVIDENCE_ITEMS


def test_relationship_context_preserves_direction_and_excludes_inactive_edges():
    source, target, other = ConceptFactory(), ConceptFactory(), ConceptFactory()
    relation(source, target)
    relation(target, source, 'Mapped from')
    relation(source, target, 'Is a', invalid_reason='D')
    relation(source, target, 'Subsumes', valid_end_date='2000-01-01')
    relation(source, target, 'Future relation', valid_start_date='2090-01-01')
    relation(other, target, 'Has ingredient')
    hits = enrich_candidates([candidate(target)], '', source.pk, min_similarity=0.3)
    edges = hits[0]['source_relationships']
    assert [(e['from_concept_id'], e['to_concept_id'], e['relationship_id']) for e in edges] == [
        (source.pk, target.pk, 'Maps to'), (target.pk, source.pk, 'Mapped from'),
    ]


def test_relationship_limits_are_per_candidate():
    source, first, second = ConceptFactory(), ConceptFactory(), ConceptFactory()
    for i in range(6):
        relation(source, first, f'Relation {i}')
    relation(source, first, 'Maps to')
    relation(source, second, 'Maps to')
    hits = enrich_candidates([candidate(first), candidate(second)], '', source.pk, min_similarity=0.3)
    assert len(hits[0]['source_relationships']) == MAX_EVIDENCE_ITEMS
    assert hits[0]['source_relationships'][0]['relationship_id'] == 'Maps to'
    assert len(hits[1]['source_relationships']) == 1


def test_enrichment_failure_keeps_candidates_and_transaction_usable():
    concept = ConceptFactory()

    def fail_synonyms(execute, sql, params, many, context):
        if 'concept_synonym' in sql:
            return execute('SELECT * FROM missing_context_test_table', None, False, context)
        return execute(sql, params, many, context)

    with connection.execute_wrapper(fail_synonyms):
        hits = enrich_candidates([candidate(concept)], 'source assay', None, min_similarity=0.3)
    assert hits[0]['concept_id'] == concept.pk
    assert hits[0]['matched_synonyms'] == []
    assert ConceptRelationship.objects.count() == 0


@pytest.mark.parametrize('entrypoint', ['batch', 'dialog', 'ingestion'])
def test_all_source_labels_and_namespace_reach_ranker(entrypoint, monkeypatch, llm):
    source = ConceptFactory(concept_code='123-4', concept_name='Canonical source name', standard_concept=None)
    target = ConceptFactory(concept_name='Candidate destination')
    synonym(target, 'Original local description')
    relation(source, target, 'Is a')
    release = UmlsRelease.objects.create(release_version='context-test')
    cui = UmlsConcept.objects.create(cui='C123', preferred_name='UMLS source name', release=release)
    UmlsSourceCode.objects.create(concept=cui, root_source='LNC', code='123-4',
                                 name='UMLS source name', is_preferred=True)
    monkeypatch.setattr(suggest, 'lexical_candidates', Mock(return_value=[candidate(target)]))
    if entrypoint == 'batch':
        SourceCodeConceptMapping.objects.create(
            source_code='123-4', source_vocabulary_id='LOINC',
            source_code_description='Original local description', source_concept=source,
            omop_table='measurement', domain_id='Measurement', occurrence_count=12,
        )
        suggest.suggest_mappings('measurement', strategies=['lexical'])
    elif entrypoint == 'dialog':
        suggest.suggest_one_mapping('123-4', 'LOINC', 'measurement',
                                    source_description='Original local description', strategies=['lexical'])
    else:
        suggest.suggest_source_code(source_code='123-4', source_vocabulary_id='LOINC',
                                    source_text='Original local description', omop_table='measurement')
    evidence = payload(llm)
    assert evidence['source']['vocabulary_id'] == 'LOINC'
    assert evidence['source']['original_description'] == 'Original local description'
    assert evidence['source']['loaded_source_concept']['name'] == 'Canonical source name'
    assert evidence['source']['umls_preferred_name'] == 'UMLS source name'
    assert evidence['candidates'][0]['matched_synonyms'][0]['text'] == 'Original local description'
    assert evidence['candidates'][0]['source_relationships'][0]['relationship_id'] == 'Is a'
    assert 'lexical_score' not in evidence['candidates'][0]
    assert 'semantic_score' not in evidence['candidates'][0]


def test_ranker_has_no_database_reads_and_keeps_explanation(llm, django_assert_num_queries):
    concept = ConceptFactory()
    context = build_source_context(source_code='LOCAL', vocabulary_id='', description='Local description',
                                   source_concept=None, umls_name='', domain_id='Measurement',
                                   omop_table='measurement')
    with django_assert_num_queries(0):
        chosen, note = suggest.rank_candidates('LOCAL', [candidate(concept)], source_context=context)
    assert chosen is None
    assert 'Conflicting source labels' in note
    assert payload(llm)['source']['vocabulary_id'] is None
    assert payload(llm)['source']['loaded_source_concept'] is None


def test_umls_evidence_is_specific_to_each_candidate():
    release = UmlsRelease.objects.create(release_version='context-test')
    snomed = VocabularyFactory(vocabulary_id='SNOMED')
    first, second = ConceptFactory(vocabulary=snomed), ConceptFactory(vocabulary=snomed)
    for label, target in [('C111', first), ('C222', second)]:
        cui = UmlsConcept.objects.create(cui=label, preferred_name=label, release=release)
        UmlsSourceCode.objects.create(concept=cui, root_source='ICD10CM', code='X')
        UmlsSourceCode.objects.create(concept=cui, root_source='SNOMEDCT_US', code=target.concept_code)
    hits, cuis = suggest.umls_candidates('X', 'ICD10CM')
    assert cuis == 'C111,C222'
    assert {c['concept_id']: c['umls_cuis'] for c in hits} == {first.pk: ['C111'], second.pk: ['C222']}
