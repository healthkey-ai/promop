from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from omop_core.models import (
    ConceptSynonym, FieldConceptMapping, SourceCodeConceptMapping, SuggestRun,
    UmlsConcept, UmlsRelease, UmlsSourceCode,
)
from omop_core.mapping.reverse_suggestions import reverse_retrieval_pool, reverse_rank_candidate
from omop_core.services.suggest_jobs import FakeDispatcher, InlineDispatcher, execute_run, use_dispatcher
from tests.factories import ConceptFactory, MeasurementFactory, VocabularyFactory

pytestmark = pytest.mark.django_db
BASE = '/api/v1/concept-to-code/'


@pytest.fixture
def client(django_user_model):
    instance = APIClient()
    instance.user = django_user_model.objects.create_user(email='concept-curator@test.com', is_staff=True)
    instance.force_authenticate(instance.user)
    return instance


@pytest.fixture
def concept():
    concept = ConceptFactory(concept_name='Serum albumin', concept_code='ALBUMIN')
    FieldConceptMapping.objects.create(concept=concept, field_name='albumin', omop_table='measurement')
    return concept


def source(code, **overrides):
    return SourceCodeConceptMapping.objects.create(**{
        'source_code': code, 'source_vocabulary_id': 'SNOMED', 'source_code_description': 'Serum albumin',
        'domain_id': 'Measurement', 'omop_table': 'measurement', 'occurrence_count': 1,
        **overrides,
    })


def apply(client, concept, row, **data):
    return client.post(f'{BASE}{concept.pk}/mappings/{row.pk}/', {
        'expected_updated_at': row.updated_at.isoformat(), **data,
    }, format='json')


def test_concepts_are_deduplicated_and_counts_do_not_multiply_by_field(client, concept, django_assert_num_queries):
    FieldConceptMapping.objects.create(concept=concept, field_name='albumin_second', omop_table='measurement')
    source('PROPOSED', target_concept=concept)
    source('APPROVED', target_concept=concept, status='approved')
    source('REJECTED', target_concept=concept, status='rejected')
    # One page, one field query, one domain summary, one priority count; no
    # per-concept reads.
    from patient_portal.api.concept_to_code import concept_to_code_list
    request = APIRequestFactory().get(BASE)
    force_authenticate(request, user=client.user)
    with django_assert_num_queries(5):
        response = concept_to_code_list(request)
    assert response.status_code == 200
    assert response.data['total'] == 1
    item = response.data['results'][0]
    assert len(item['fields']) == 2
    assert item['sccm_counts'] == {'approved': 1, 'proposed': 1, 'rejected': 1}
    assert item['seen'] == 3
    assert client.get(BASE, {'search': 'albumin_second'}).data['total'] == 1
    assert client.get(BASE, {'domain': 'condition'}).data['total'] == 0
    assert client.get(BASE, {'status': 'approved'}).data['total'] == 0


def test_all_scope_reaches_non_field_destinations_but_only_current_standard(client, concept):
    other = ConceptFactory(concept_name='Other standard')
    nonstandard = ConceptFactory(standard_concept=None)
    retired = ConceptFactory(invalid_reason='D')
    future = ConceptFactory(valid_start_date='2090-01-01')
    for index, invalid in enumerate([nonstandard, retired, future]):
        FieldConceptMapping.objects.create(concept=invalid, field_name=f'invalid_{index}')
    assert client.get(BASE).data['total'] == 1
    response = client.get(BASE, {'scope': 'all'})
    assert {item['concept_id'] for item in response.data['results']} == {concept.pk, other.pk}
    assert client.get(BASE, {'scope': 'all', 'search': str(other.pk)}).data['total'] == 1
    assert client.get(f'{BASE}{nonstandard.pk}/').status_code == 404


def test_list_and_detail_paginate_before_serialization(client, concept):
    for index in range(52):
        source(f'C{index:02}', target_concept=concept, occurrence_count=index)
    data = client.get(f'{BASE}{concept.pk}/', {'seen_only': '1'}).data
    assert data['total'] == 51 and len(data['results']) == 50
    assert data['results'][0]['source_code'] == 'C51'
    assert data['zero_seen'] == 1
    second = client.get(f'{BASE}{concept.pk}/', {'seen_only': '1', 'page': 2}).data
    assert second['results'][0]['source_code'] == 'C01'
    assert client.get(f'{BASE}{concept.pk}/', {'seen_only': '0'}).data['total'] == 52
    assert client.get(BASE, {'page': 'bad'}).status_code == 400


def test_available_sources_include_nonstandard_imports_and_protect_decisions(client, concept):
    bad = ConceptFactory(standard_concept=None)
    imported = source('IMPORTED', target_concept=bad, origin_system='HT-One')
    source('GAP')
    source('ALREADY', target_concept=concept)
    source('APPROVED', status='approved')
    source('REJECTED', status='rejected')
    source('REFERENCE', origin_system='athena')
    source('WRONG-DOMAIN', domain_id='Drug')
    source('WRONG-TABLE', omop_table='drug_exposure')
    data = client.get(f'{BASE}{concept.pk}/', {'mode': 'available'}).data
    assert {row['source_code'] for row in data['results']} == {'IMPORTED', 'GAP'}
    assert client.get(f'{BASE}{concept.pk}/', {'mode': 'available', 'search': 'IMPORTED'}).data['total'] == 1
    imported.refresh_from_db()
    assert imported.target_concept == bad


def test_lexical_uses_synonyms_and_seen_priority_without_writing(concept):
    ConceptFactory(concept_id=4180186, concept_code='ENGLISH')
    ConceptSynonym.objects.create(concept=concept, concept_synonym_name='Blood albumin', language_concept_id=4180186)
    source('SEEN', occurrence_count=10, source_code_description='Blood albumin')
    source('ZERO', occurrence_count=0)
    source('UNRELATED', source_code_description='Bone marrow sampling')
    candidates = reverse_retrieval_pool(concept, strategies=['lexical'], limit=10)
    assert [row['source_code'] for row in candidates] == ['SEEN']
    assert candidates[0]['evidence'] == ['lexical']
    all_codes = reverse_retrieval_pool(concept, strategies=['lexical'], limit=10, include_zero_seen=True)
    assert [row['source_code'] for row in all_codes] == ['SEEN', 'ZERO']
    assert not SourceCodeConceptMapping.objects.exclude(target_concept=None).exists()


def test_umls_matches_vocabulary_and_code_including_aliases(concept):
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C0000001', release=release)
    for root, code in [('LNC', concept.concept_code), ('SNOMEDCT_US', '123')]:
        UmlsSourceCode.objects.create(concept=cui, root_source=root, code=code, term_type='PT', name='Albumin')
    source('123', source_code_description='Different label')
    source('123', source_vocabulary_id='LOINC')
    source('123', source_vocabulary_id='urn:oid:2.16.840.1.113883.6.96', occurrence_count=2)
    candidates = reverse_retrieval_pool(concept, strategies=['umls'], limit=10)
    assert [row['source_vocabulary_id'] for row in candidates] == ['urn:oid:2.16.840.1.113883.6.96', 'SNOMED']


def test_loinc_retrieval_reaches_short_analyte_labels_without_synonyms():
    albumin = ConceptFactory(
        concept_id=3024561, concept_code='1751-7',
        concept_name='Albumin [Mass/volume] in Serum or Plasma',
    )
    short = source('albumin', source_vocabulary_id='', source_code_description='Albumin (g/dL)', occurrence_count=0)
    seen = source('serum albumin', source_vocabulary_id='', source_code_description='Serum albumin', occurrence_count=5)
    source('UNRELATED', source_code_description='Bone marrow sampling', occurrence_count=100)
    source('APPROVED', source_code_description='Albumin', status='approved')
    source('REJECTED', source_code_description='Albumin', status='rejected')
    source('REFERENCE', source_code_description='Albumin', origin_system='athena')
    source('WRONG-DOMAIN', source_code_description='Albumin', domain_id='Drug')
    source('ALREADY-LINKED', source_code_description='Albumin', target_concept=albumin)

    assert [row['mapping_id'] for row in reverse_retrieval_pool(
        albumin, strategies=['lexical'], limit=25,
    )] == [seen.pk]
    candidates = reverse_retrieval_pool(albumin, strategies=['lexical'], limit=25, include_zero_seen=True)
    assert [row['mapping_id'] for row in candidates] == [seen.pk, short.pk]
    assert candidates[1]['evidence'] == ['lexical']
    short.refresh_from_db()
    assert short.target_concept_id is None and short.status == 'proposed'


def test_suggest_passes_short_loinc_candidates_to_the_ranker(client):
    albumin = ConceptFactory(
        concept_id=3024561, concept_code='1751-7',
        concept_name='Albumin [Mass/volume] in Serum or Plasma',
    )
    row = source('albumin', source_vocabulary_id='', source_code_description='Albumin (g/dL)', occurrence_count=0)
    with patch('omop_core.mapping.suggestions.rank_candidates_dispatch', return_value=(None, 'Needs review', [], {})) as rank:
        with use_dispatcher(InlineDispatcher()):
            response = client.post(f'{BASE}suggest/', {
                'concept_ids': [albumin.pk], 'strategies': ['umls', 'lexical'],
                'include_zero_seen': True, 'ranking_model': 'anthropic',
            }, format='json')
    assert response.status_code == 202
    assert response.data['state'] == 'success'
    assert response.data['activity'][0]['candidates'][0]['mapping_id'] == row.pk
    rank.assert_called_once()
    assert rank.call_args.args[0] == 'albumin'
    assert rank.call_args.args[1][0]['concept_name'] == albumin.concept_name
    assert rank.call_args.kwargs['require_model_selection'] is True
    row.refresh_from_db()
    assert row.target_concept_id is None and row.status == 'proposed'


def test_suggest_dispatch_persisted_poll_and_no_mapping_mutations(client, concept):
    row = source('A')
    dispatcher = FakeDispatcher()
    with use_dispatcher(dispatcher):
        response = client.post(f'{BASE}suggest/', {'concept_ids': [concept.pk], 'strategies': ['lexical']}, format='json')
    assert response.status_code == 202
    run_id, params = dispatcher.calls[0]
    assert SuggestRun.objects.get(pk=run_id).direction == 'reverse'
    execute_run(run_id, params)
    finished = client.get(f'{BASE}suggest-runs/{run_id}/').data
    assert finished['state'] == 'success'
    assert finished['activity'][0]['candidates'][0]['mapping_id'] == row.pk
    assert finished['activity'][0]['candidates'][0]['verdict'] == 'review'
    row.refresh_from_db()
    assert row.target_concept_id is None and row.status == 'proposed'
    with patch('omop_core.services.reverse_suggest_jobs.reverse_retrieval_pool') as retrieval:
        execute_run(run_id, params)
        retrieval.assert_not_called()


def test_ranker_cannot_fallback_to_first_candidate_without_model_selection(concept):
    with patch('omop_core.mapping.suggestions.rank_candidates_dispatch', return_value=(None, 'Unavailable', [], {})) as rank:
        candidate = reverse_rank_candidate(concept, {'source_code': 'A', 'source_code_description': 'Albumin', 'source_vocabulary_id': 'SNOMED'}, 'anthropic')
    assert candidate['verdict'] == 'review'
    assert rank.call_args.kwargs['require_model_selection'] is True


def test_worker_redelivery_marks_interrupted_preview_failed_and_keeps_results(concept):
    from omop_core.tasks import suggest_mappings_task
    preview = [{'concept': {'concept_id': concept.pk}, 'candidates': []}]
    run = SuggestRun.objects.create(direction='reverse', state=SuggestRun.RUNNING, activity=preview)
    suggest_mappings_task.push_request(delivery_info={'redelivered': True})
    try:
        result = suggest_mappings_task.run(str(run.pk), {'direction': 'reverse'})
    finally:
        suggest_mappings_task.pop_request()
    run.refresh_from_db()
    assert result['state'] == 'failure'
    assert run.activity == preview and run.finished_at is not None
    assert 'interrupted' in run.error and 'retry' in run.error


def test_worker_redelivery_before_start_still_executes_the_preview(client, concept):
    from omop_core.tasks import suggest_mappings_task
    dispatcher = FakeDispatcher()
    with use_dispatcher(dispatcher):
        client.post(f'{BASE}suggest/', {'concept_ids': [concept.pk], 'strategies': ['lexical']}, format='json')
    run_id, params = dispatcher.calls[0]
    suggest_mappings_task.push_request(delivery_info={'redelivered': True})
    try:
        assert suggest_mappings_task.run(run_id, params)['state'] == 'success'
        # A completed preview stays successful on another redelivery.
        assert suggest_mappings_task.run(run_id, params)['state'] == 'success'
    finally:
        suggest_mappings_task.pop_request()


@pytest.mark.parametrize('payload', [
    {'concept_ids': []}, {'concept_ids': [True]}, {'concept_ids': ['1']},
    {'strategies': []}, {'strategies': ['invalid']}, {'limit': 0}, {'limit': True},
    {'include_zero_seen': 'false'}, {'ranking_model': 'bad'},
])
def test_suggest_validates_request(client, concept, payload):
    response = client.post(f'{BASE}suggest/', {'concept_ids': [concept.pk], **payload}, format='json')
    assert response.status_code == 400


def test_inline_ranking_budget_is_per_candidate(client, concept):
    with patch.object(InlineDispatcher, 'dispatch'), use_dispatcher(InlineDispatcher()):
        response = client.post(f'{BASE}suggest/', {'concept_ids': [concept.pk], 'limit': 100, 'ranking_model': 'anthropic'}, format='json')
    assert response.status_code == 202
    assert response.data['selection']['limit'] == 3
    assert response.data['selection']['retrieval_timeout_ms'] == 8000


def test_inline_preview_completes_without_a_broker(client, concept):
    source('A')
    with use_dispatcher(InlineDispatcher()):
        response = client.post(f'{BASE}suggest/', {'concept_ids': [concept.pk], 'strategies': ['lexical']}, format='json')
    assert response.status_code == 202
    assert response.data['state'] == 'success'
    assert len(response.data['activity'][0]['candidates']) == 1


def test_search_failures_are_visible_and_keep_completed_previews(client, concept):
    other = ConceptFactory(concept_name='Other standard')
    dispatcher = FakeDispatcher()
    with use_dispatcher(dispatcher):
        response = client.post(f'{BASE}suggest/', {'concept_ids': [concept.pk, other.pk]}, format='json')
    run_id, params = dispatcher.calls[0]
    with patch('omop_core.services.reverse_suggest_jobs.reverse_retrieval_pool', side_effect=[[], TimeoutError('Budget exhausted')]):
        execute_run(run_id, params)
    run = SuggestRun.objects.get(pk=run_id)
    assert run.state == 'failure' and run.done == 1 and run.finished_at is not None
    assert len(run.activity) == 1
    assert 'retry' in client.get(f'{BASE}suggest-runs/{run_id}/').data['error']


def test_retrieval_restores_the_callers_statement_timeout(concept):
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        before = cursor.fetchone()[0]
    reverse_retrieval_pool(concept, strategies=['lexical'], limit=3, timeout_ms=5000)
    with connection.cursor() as cursor:
        cursor.execute('SHOW statement_timeout')
        assert cursor.fetchone()[0] == before


# ── Estimate: how many sources Suggest could find (#1953) ─────────────────────

@pytest.fixture(autouse=True)
def _fresh_estimate_cache():
    # Exact estimates are cached as the fallback for a timed-out count (#1963).
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


def test_estimate_counts_every_source_a_run_could_return(client, concept):
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C0000001', release=release)
    for root, code in [('LNC', concept.concept_code), ('SNOMEDCT_US', 'UMLS-ONLY')]:
        UmlsSourceCode.objects.create(concept=cui, root_source=root, code=code, term_type='PT', name='Albumin')
    source('UMLS-ONLY', source_code_description='Different label')
    for index in range(30):
        source(f'LEX{index:02}', occurrence_count=index + 1)
    source('ZERO', occurrence_count=0)
    source('UNRELATED', source_code_description='Bone marrow sampling')
    source('DONE', target_concept=concept)
    source('APPROVED', status='approved')

    data = client.get(f'{BASE}{concept.pk}/estimate/').data
    # More than one run's worth: 30 by name, one by UMLS, none counted twice.
    assert data == {'total': 31, 'umls': 1, 'lexical': 30, 'capped': False, 'partial': False,
                    'approximate': None, 'timed_out': False}
    assert len(reverse_retrieval_pool(concept, strategies=['umls', 'lexical'], limit=25)) == 25
    assert client.get(f'{BASE}{concept.pk}/estimate/', {'include_zero_seen': '1'}).data['total'] == 32
    assert client.get(f'{BASE}{concept.pk}/estimate/', {'strategies': 'umls'}).data['total'] == 1
    assert not SourceCodeConceptMapping.objects.filter(target_concept=concept).exclude(source_code='DONE').exists()


def test_estimate_counts_a_source_found_by_both_strategies_once(client, concept):
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C0000001', release=release)
    for root, code in [('LNC', concept.concept_code), ('SNOMEDCT_US', 'BOTH')]:
        UmlsSourceCode.objects.create(concept=cui, root_source=root, code=code, term_type='PT', name='Albumin')
    source('BOTH')
    data = client.get(f'{BASE}{concept.pk}/estimate/').data
    assert (data['total'], data['umls'], data['lexical']) == (1, 1, 1)


def test_estimate_counts_unregistered_vocabulary_codes_only_with_zero_seen(client, concept):
    VocabularyFactory(vocabulary_id='SNOMED')
    ConceptFactory(vocabulary_id='SNOMED', concept_code='VOCAB-ONLY', concept_name='Serum albumin level')
    ConceptFactory(vocabulary_id='SNOMED', concept_code='REGISTERED', concept_name='Serum albumin measurement')
    source('REGISTERED', occurrence_count=0)
    source('DECIDED', status='approved')
    ConceptFactory(vocabulary_id='SNOMED', concept_code='DECIDED', concept_name='Serum albumin test')
    assert client.get(f'{BASE}{concept.pk}/estimate/').data['total'] == 0
    data = client.get(f'{BASE}{concept.pk}/estimate/', {'include_zero_seen': '1'}).data
    # The registered code is its mapping row, counted once; the decided one is skipped.
    assert data['total'] == 2 and data['capped'] is False
    run = {row['source_code'] for row in reverse_retrieval_pool(
        concept, strategies=['umls', 'lexical'], limit=25, include_zero_seen=True)}
    assert run == {'VOCAB-ONLY', 'REGISTERED'}


def test_estimate_counts_an_open_mapping_row_found_only_by_its_vocabulary_name(client, concept):
    VocabularyFactory(vocabulary_id='SNOMED')
    ConceptFactory(vocabulary_id='SNOMED', concept_code='TERSE', concept_name='Serum albumin level')
    source('TERSE', occurrence_count=0, source_code_description='ALB SER')
    run = reverse_retrieval_pool(concept, strategies=['lexical'], limit=25, include_zero_seen=True)
    assert [row['source_code'] for row in run] == ['TERSE']
    data = client.get(f'{BASE}{concept.pk}/estimate/', {'strategies': 'lexical', 'include_zero_seen': '1'}).data
    assert (data['total'], data['lexical']) == (1, 1)


def test_estimate_reports_a_floor_past_the_catalog_cap(client, concept):
    VocabularyFactory(vocabulary_id='SNOMED')
    for index in range(4):
        ConceptFactory(vocabulary_id='SNOMED', concept_code=f'VOCAB-{index}', concept_name=f'Serum albumin {index}')
    with patch('omop_core.mapping.reverse_catalog.CATALOG_CAP', 3):
        data = client.get(f'{BASE}{concept.pk}/estimate/', {'include_zero_seen': '1'}).data
    assert data['capped'] is True and data['total'] == 3


def test_estimate_keeps_registered_counts_when_the_vocabulary_search_runs_out_of_time(client, concept):
    source('SEEN')
    source('ZERO', occurrence_count=0)
    with patch('omop_core.mapping.reverse_suggestions.catalog_keys', side_effect=TimeoutError('Budget exhausted')):
        data = client.get(f'{BASE}{concept.pk}/estimate/', {'include_zero_seen': '1'}).data
    assert data['total'] == 2 and data['capped'] is True and data['partial'] is True


def test_a_timed_out_estimate_falls_back_to_the_last_exact_count(client, concept):
    for index in range(30):
        source(f'LEX{index:02}')
    assert client.get(f'{BASE}{concept.pk}/estimate/').data['total'] == 30
    source('LEX30')
    with patch('omop_core.mapping.reverse_suggestions._estimate', side_effect=TimeoutError('Budget exhausted')):
        data = client.get(f'{BASE}{concept.pk}/estimate/').data
    # The earlier count, off by what changed since, and said to be approximate.
    assert (data['total'], data['approximate'], data['timed_out']) == (30, 'earlier', False)
    # Only for the same settings: Seen > 0 off is a different count.
    with patch('omop_core.mapping.reverse_suggestions._estimate', side_effect=TimeoutError('Budget exhausted')), \
            patch('omop_core.mapping.reverse_suggestions.SAMPLE_PERCENT', 100):
        other = client.get(f'{BASE}{concept.pk}/estimate/', {'include_zero_seen': '1'}).data
    assert other['approximate'] == 'sample'


def test_a_timed_out_estimate_with_no_earlier_count_is_sampled(client, concept):
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C0000001', release=release)
    for root, code in [('LNC', concept.concept_code), ('SNOMEDCT_US', 'UMLS-ONLY')]:
        UmlsSourceCode.objects.create(concept=cui, root_source=root, code=code, term_type='PT', name='Albumin')
    source('UMLS-ONLY', source_code_description='Different label')
    for index in range(30):
        source(f'LEX{index:02}')
    source('UNRELATED', source_code_description='Bone marrow sampling')
    # A sample of every page counts what the exact query counts.
    with patch('omop_core.mapping.reverse_suggestions._estimate', side_effect=TimeoutError('Budget exhausted')), \
            patch('omop_core.mapping.reverse_suggestions.SAMPLE_PERCENT', 100):
        data = client.get(f'{BASE}{concept.pk}/estimate/').data
        zero_seen = client.get(f'{BASE}{concept.pk}/estimate/', {'include_zero_seen': '1'}).data
    assert data == {'total': 31, 'umls': 1, 'lexical': 30, 'capped': False, 'partial': False,
                    'approximate': 'sample', 'timed_out': False}
    # Never-seen vocabulary codes are not sampled, so that total is a floor.
    assert zero_seen['partial'] is True


@pytest.mark.parametrize('value, rounded', [(0, 0), (37, 37), (6460, 6500), (1040, 1000), (123456, 120000)])
def test_a_sampled_count_is_rounded_to_two_figures(value, rounded):
    from omop_core.mapping.reverse_suggestions import _two_figures
    assert _two_figures(value) == rounded


def test_estimate_survives_an_unreachable_cache(client, concept):
    source('SEEN')
    with patch('omop_core.mapping.reverse_suggestions.cache') as down:
        down.get.side_effect = down.set.side_effect = ConnectionError('cache down')
        data = client.get(f'{BASE}{concept.pk}/estimate/').data
    assert (data['total'], data['approximate']) == (1, None)


def test_estimate_says_when_it_ran_out_of_time(client, concept):
    with patch('omop_core.mapping.reverse_suggestions._estimate', side_effect=TimeoutError('Budget exhausted')), \
            patch('omop_core.mapping.reverse_suggestions._sampled', side_effect=TimeoutError('Budget exhausted')):
        response = client.get(f'{BASE}{concept.pk}/estimate/')
    assert response.status_code == 200
    assert response.data == {'total': None, 'timed_out': True}


def test_estimate_validates_and_requires_curator_access(client, concept, django_user_model):
    assert client.get(f'{BASE}{concept.pk}/estimate/', {'strategies': 'vectors'}).status_code == 400
    assert client.get(f'{BASE}{ConceptFactory(standard_concept=None).pk}/estimate/').status_code == 404
    client.force_authenticate(django_user_model.objects.create_user(email='estimate-patient@test.com'))
    assert client.get(f'{BASE}{concept.pk}/estimate/').status_code == 403
    client.force_authenticate(None)
    assert client.get(f'{BASE}{concept.pk}/estimate/').status_code in (401, 403)


def test_concept_list_is_bounded(client):
    for index in range(52):
        ConceptFactory(concept_name=f'Paged {index:02}')
    response = client.get(BASE, {'scope': 'all', 'search': 'Paged', 'page': 2})
    assert response.data['total'] == 52
    assert len(response.data['results']) == 2


def test_approval_reuses_clinical_repoint_and_stamps_reviewer(client, concept, django_capture_on_commit_callbacks):
    original = ConceptFactory(standard_concept=None)
    row = source('IMPORTED', target_concept=original, notes='Original import evidence')
    with patch('omop_core.services.repoint_jobs.repoint_clinical_rows',
               side_effect=lambda **kwargs: {'rows_updated': 1, 'rows_collapsed': 0, 'person_ids': {7}}) as repoint, \
            django_capture_on_commit_callbacks(execute=True):
        response = apply(client, concept, row, status='approved')
    assert response.status_code == 200, response.data
    assert {call.kwargs['old_concept_id'] for call in repoint.call_args_list} == {0, original.pk}
    row.refresh_from_db()
    assert row.target_concept == concept and row.status == 'approved'
    assert row.reviewer is not None and row.reviewed_at is not None
    assert row.destination_vocabulary_id == concept.vocabulary_id
    assert row.notes == 'Original import evidence'
    assert response.data['repoint'] == {'queued': True, 'from_concept_ids': sorted([0, original.pk])}


def test_proposal_does_not_write_clinical_rows_and_can_be_approved_later(client, concept):
    row = source('A')
    with patch('patient_portal.api.views.repoint_clinical_rows') as repoint:
        response = apply(client, concept, row)
    assert response.status_code == 200
    repoint.assert_not_called()
    row.refresh_from_db()
    assert row.target_concept == concept and row.status == 'proposed'
    assert apply(client, concept, row, status='approved').status_code == 200


@pytest.mark.parametrize('propose_first', [False, True])
def test_approval_moves_stored_imports_and_unresolved_rows_only(client, concept, propose_first,
                                                               django_capture_on_commit_callbacks):
    original = ConceptFactory(standard_concept=None)
    corrected = ConceptFactory()
    row = source('IMPORTED', target_concept=original, origin='import')
    stored = MeasurementFactory(measurement_concept=original, measurement_source_value='IMPORTED')
    unresolved = MeasurementFactory(measurement_concept_id=0, measurement_source_value='IMPORTED')
    unrelated = MeasurementFactory(measurement_concept=original, measurement_source_value='OTHER')
    manual = MeasurementFactory(measurement_concept=corrected, measurement_source_value='IMPORTED')
    if propose_first:
        assert apply(client, concept, row).status_code == 200
        row.refresh_from_db()
        stored.refresh_from_db()
        assert stored.measurement_concept_id == original.pk
    with django_capture_on_commit_callbacks(execute=True):
        response = apply(client, concept, row, status='approved')
    assert response.status_code == 200, response.data
    for fact in (stored, unresolved, unrelated, manual):
        fact.refresh_from_db()
    assert stored.measurement_concept_id == concept.pk
    assert unresolved.measurement_concept_id == concept.pk
    assert unrelated.measurement_concept_id == original.pk
    assert manual.measurement_concept_id == corrected.pk
    assert response.data['repoint']['queued'] is True
    row.refresh_from_db()
    assert row.pending_repoint_concept_ids == [] and row.deferred_repoint_concept_ids == []


def test_proposal_history_survives_another_edit_and_approval_in_source_editor(client, concept,
                                                                            django_capture_on_commit_callbacks):
    original = ConceptFactory(standard_concept=None)
    next_destination = ConceptFactory()
    row = source('IMPORTED', target_concept=original, origin='import')
    stored = MeasurementFactory(measurement_concept=original, measurement_source_value='IMPORTED')
    assert apply(client, concept, row).status_code == 200
    row.refresh_from_db()
    assert row.pending_repoint_concept_ids == [original.pk]
    assert apply(client, next_destination, row).status_code == 200
    row.refresh_from_db()
    assert row.pending_repoint_concept_ids == [original.pk, concept.pk]
    # The pending destinations belong to this exact source, not a new code.
    response = client.patch(f'/api/v1/code-mappings/{row.pk}/', {
        'source_code': 'DIFFERENT',
    }, format='json')
    assert response.status_code == 400
    with django_capture_on_commit_callbacks(execute=True):
        response = client.patch(f'/api/v1/code-mappings/{row.pk}/', {'status': 'approved'}, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db(); stored.refresh_from_db()
    assert stored.measurement_concept_id == next_destination.pk
    assert row.pending_repoint_concept_ids == []


def test_approval_returns_before_rows_move_and_a_failed_job_keeps_the_work(
        client, concept, django_capture_on_commit_callbacks):
    """The save writes the mapping; stored rows move after commit, in a job.

    A job that fails leaves the destinations queued on the mapping, and the next
    run moves them.
    """
    from omop_core.services.repoint_jobs import run_repoint
    original = ConceptFactory(standard_concept=None)
    row = source('IMPORTED', target_concept=original)
    stored = MeasurementFactory(measurement_concept=original, measurement_source_value='IMPORTED')
    with django_capture_on_commit_callbacks() as callbacks:
        response = apply(client, concept, row, status='approved')
    assert response.status_code == 200, response.data
    row.refresh_from_db(); stored.refresh_from_db()
    # Written, approved, and nothing moved yet.
    assert row.target_concept_id == concept.pk and row.status == 'approved'
    assert stored.measurement_concept_id == original.pk
    assert row.deferred_repoint_concept_ids == sorted([0, original.pk])
    assert len(callbacks) == 1

    with patch('omop_core.services.repoint_jobs.repoint_clinical_rows', side_effect=RuntimeError('write failed')):
        with pytest.raises(RuntimeError):
            run_repoint(row.pk)
    row.refresh_from_db()
    assert row.deferred_repoint_concept_ids == sorted([0, original.pk])

    assert run_repoint(row.pk)['rows_updated'] == 1
    row.refresh_from_db(); stored.refresh_from_db()
    assert stored.measurement_concept_id == concept.pk
    assert row.deferred_repoint_concept_ids == []


def test_json_arrays_are_rejected_without_server_errors(client, concept):
    row = source('A')
    assert client.post(f'{BASE}suggest/', [], format='json').status_code == 400
    assert client.post(f'{BASE}{concept.pk}/mappings/{row.pk}/', [], format='json').status_code == 400


def test_stale_preview_locked_source_and_existing_decisions_are_protected(client, concept, django_user_model):
    row = source('A')
    stale = row.updated_at.isoformat()
    row.notes = 'Someone edited this'; row.save()
    assert apply(client, concept, row, expected_updated_at=stale).status_code == 409
    row.locked_by = django_user_model.objects.create_user(email='other@test.com')
    row.locked_at = timezone.now(); row.save()
    assert apply(client, concept, row).status_code == 423
    row.locked_by = None; row.status = 'approved'; row.save()
    assert apply(client, concept, row).status_code == 409
    row.refresh_from_db()
    assert row.target_concept_id is None


def test_domain_mismatch_and_nonstandard_target_cannot_be_approved(client, concept):
    row = source('A', domain_id='Drug')
    assert apply(client, concept, row, status='approved').status_code == 400
    bad = ConceptFactory(standard_concept=None)
    assert apply(client, bad, row, status='approved').status_code == 404
    row.refresh_from_db()
    assert row.target_concept_id is None


def test_permissions_and_forward_run_separation(client, concept, django_user_model):
    forward = SuggestRun.objects.create(direction='forward')
    SuggestRun.objects.create(direction='reverse')
    assert client.get('/api/v1/code-mappings/suggest-runs/latest/').data['run_id'] == str(forward.pk)
    assert client.get(f'{BASE}suggest-runs/{forward.pk}/').status_code == 404
    client.force_authenticate(django_user_model.objects.create_user(email='patient@test.com'))
    assert client.get(BASE).status_code == 403
    row = source('A')
    with patch('patient_portal.api.views._can_manage_field_mappings', return_value=True):
        assert apply(client, concept, row).status_code == 200
        row.refresh_from_db()
        assert apply(client, concept, row, status='approved').status_code == 400
    row.refresh_from_db()
    assert row.status == 'proposed'


# ── Source examples: patient Source Metadata for a source code (#1915) ──────

EXAMPLES = f'{BASE}source-examples/'


def inventory(code, record, **overrides):
    from omop_core.models import PatientSourceCode
    return PatientSourceCode.objects.create(**{
        'person': record.person, 'source_value': code, 'source_vocabulary_id': 'SNOMED',
        'omop_table': 'measurement', 'occurrence_count': 1,
        'source_metadata': {'category': 'laboratory', 'person': record.person.person_id},
        **overrides,
    })


def test_source_examples_return_up_to_three_patients_matching_system_and_code(client):
    from tests.factories import PatientRecordFactory
    records = [PatientRecordFactory() for _ in range(5)]
    for index, record in enumerate(records):
        inventory('ALB', record, occurrence_count=index + 1)
    inventory('ALB', PatientRecordFactory(), source_vocabulary_id='LOINC')  # other system
    inventory('ALBX', PatientRecordFactory())                               # other code

    response = client.get(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB'})

    assert response.status_code == 200
    examples = response.data['examples']
    # Most-seen first, one entry per patient, capped at three.
    assert [example['person_id'] for example in examples] == [r.person.person_id for r in records[::-1][:3]]
    assert examples[0]['source_metadata'] == {'category': 'laboratory', 'person': records[-1].person.person_id}


def test_source_examples_apply_curator_corrections_and_match_vocabulary_aliases(client):
    from omop_core.services.source_vocabularies import VOCABULARY_OID_ALIASES
    from tests.factories import PatientRecordFactory
    alias, canonical = next(iter(VOCABULARY_OID_ALIASES.items()))
    inventory('ALB', PatientRecordFactory(), source_vocabulary_id=alias, source_unit='g/dl',
              source_unit_override='g/dL', example_quantity='4', example_quantity_override=None)

    examples = client.get(EXAMPLES, {'source_vocabulary_id': canonical, 'source_code': 'ALB'}).data['examples']

    assert len(examples) == 1
    assert examples[0]['source_unit'] == 'g/dL'
    assert examples[0]['example_quantity'] == '4'


def test_source_examples_filter_by_table_but_keep_untabled_inventory(client):
    from tests.factories import PatientRecordFactory
    measured, untabled, observed = PatientRecordFactory(), PatientRecordFactory(), PatientRecordFactory()
    inventory('ALB', measured)
    inventory('ALB', untabled, omop_table='')
    inventory('ALB', observed, omop_table='observation')

    examples = client.get(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB',
                                     'omop_table': 'measurement'}).data['examples']

    assert {example['person_id'] for example in examples} == {measured.person.person_id, untabled.person.person_id}


def test_source_examples_only_show_patients_an_org_admin_administers(django_user_model):
    from omop_core.models import GroupAccess
    from tests.factories import OrganizationFactory, PatientRecordFactory
    org, other = OrganizationFactory(), OrganizationFactory()
    admin = django_user_model.objects.create_user(email='org-admin@test.com')
    GroupAccess.objects.create(identity=admin, org=org, role='org_admin')
    mine, theirs = PatientRecordFactory(organization=org), PatientRecordFactory(organization=other)
    inventory('ALB', mine)
    inventory('ALB', theirs, occurrence_count=99)
    api = APIClient()
    api.force_authenticate(admin)

    examples = api.get(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB'}).data['examples']

    assert [example['person_id'] for example in examples] == [mine.person.person_id]


def test_source_examples_are_denied_to_non_admin_curators(django_user_model):
    from omop_core.models import GroupAccess
    from tests.factories import OrganizationFactory, PatientRecordFactory
    org = OrganizationFactory()
    doctor = django_user_model.objects.create_user(email='doctor@test.com')
    GroupAccess.objects.create(identity=doctor, org=org, role='doctor')
    inventory('ALB', PatientRecordFactory(organization=org))
    api = APIClient()
    api.force_authenticate(doctor)

    response = api.get(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB'})

    assert response.status_code == 403
    assert 'examples' not in response.data


def test_source_examples_require_authentication_and_a_code(client):
    assert APIClient().get(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB'}).status_code in (401, 403)
    assert client.get(EXAMPLES, {'source_vocabulary_id': 'SNOMED'}).status_code == 400
    assert client.get(EXAMPLES, {'source_code': 'ALB', 'omop_table': 'nonsense'}).status_code == 400


def test_source_examples_do_not_write(client):
    from omop_core.models import PatientSourceCode
    from tests.factories import PatientRecordFactory
    row = inventory('ALB', PatientRecordFactory())
    before = PatientSourceCode.objects.values().get(pk=row.pk)

    assert client.get(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB'}).status_code == 200
    assert client.post(EXAMPLES, {'source_vocabulary_id': 'SNOMED', 'source_code': 'ALB'}, format='json').status_code == 405
    assert PatientSourceCode.objects.values().get(pk=row.pk) == before


def test_linked_sources_filter_by_status_and_count_every_status(client, concept):
    source('OK', target_concept=concept, status='approved', occurrence_count=5)
    source('MAYBE', target_concept=concept, status='proposed')
    source('NO', target_concept=concept, status='rejected')
    source('UNSEEN', target_concept=concept, status='approved', occurrence_count=0)
    data = client.get(f'{BASE}{concept.pk}/', {'status': 'proposed', 'seen_only': '1'}).data
    assert [row['source_code'] for row in data['results']] == ['MAYBE']
    assert data['total'] == 1
    # Counts follow the Seen filter, so a collapsed group announces what it would show.
    assert data['status_counts'] == {'approved': 1, 'proposed': 1, 'rejected': 1}
    unfiltered = client.get(f'{BASE}{concept.pk}/', {'seen_only': '0'}).data
    assert unfiltered['total'] == 4
    assert unfiltered['status_counts'] == {'approved': 2, 'proposed': 1, 'rejected': 1}
    assert client.get(f'{BASE}{concept.pk}/', {'status': 'bogus'}).status_code == 400
    assert client.get(f'{BASE}{concept.pk}/', {'mode': 'available'}).data['status_counts'] is None
