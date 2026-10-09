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
from tests.factories import ConceptFactory, MeasurementFactory

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
    # One page, one field query, one domain summary; no per-concept reads.
    from patient_portal.api.concept_to_code import concept_to_code_list
    request = APIRequestFactory().get(BASE)
    force_authenticate(request, user=client.user)
    with django_assert_num_queries(4):
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


def test_concept_list_is_bounded(client):
    for index in range(52):
        ConceptFactory(concept_name=f'Paged {index:02}')
    response = client.get(BASE, {'scope': 'all', 'search': 'Paged', 'page': 2})
    assert response.data['total'] == 52
    assert len(response.data['results']) == 2


def test_approval_reuses_clinical_repoint_and_stamps_reviewer(client, concept):
    original = ConceptFactory(standard_concept=None)
    row = source('IMPORTED', target_concept=original, notes='Original import evidence')
    with patch('patient_portal.api.views.repoint_clinical_rows', side_effect=lambda **kwargs: {'rows_updated': 1, 'person_ids': {7}}) as repoint:
        response = apply(client, concept, row, status='approved')
    assert response.status_code == 200, response.data
    assert {call.kwargs['old_concept_id'] for call in repoint.call_args_list} == {0, original.pk}
    row.refresh_from_db()
    assert row.target_concept == concept and row.status == 'approved'
    assert row.reviewer is not None and row.reviewed_at is not None
    assert row.destination_vocabulary_id == concept.vocabulary_id
    assert row.notes == 'Original import evidence'
    assert response.data['repoint']['persons_marked_stale'] == 1


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
def test_approval_moves_stored_imports_and_unresolved_rows_only(client, concept, propose_first):
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
    response = apply(client, concept, row, status='approved')
    assert response.status_code == 200, response.data
    for fact in (stored, unresolved, unrelated, manual):
        fact.refresh_from_db()
    assert stored.measurement_concept_id == concept.pk
    assert unresolved.measurement_concept_id == concept.pk
    assert unrelated.measurement_concept_id == original.pk
    assert manual.measurement_concept_id == corrected.pk
    assert response.data['repoint']['rows_updated'] == 2
    row.refresh_from_db()
    assert row.pending_repoint_concept_ids == []


def test_proposal_history_survives_another_edit_and_approval_in_source_editor(client, concept):
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
    response = client.patch(f'/api/v1/code-mappings/{row.pk}/', {'status': 'approved'}, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db(); stored.refresh_from_db()
    assert stored.measurement_concept_id == next_destination.pk
    assert row.pending_repoint_concept_ids == []


def test_failed_clinical_update_rolls_back_mapping_approval(client, concept):
    original = ConceptFactory(standard_concept=None)
    row = source('IMPORTED', target_concept=original)
    revision = row.updated_at
    with patch('patient_portal.api.views.repoint_clinical_rows', side_effect=RuntimeError('Database write failed')):
        with pytest.raises(RuntimeError, match='Database write failed'):
            apply(client, concept, row, status='approved')
    row.refresh_from_db()
    assert row.target_concept_id == original.pk and row.status == 'proposed'
    assert row.updated_at == revision and row.reviewer_id is None


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
