from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from omop_core.mapping.reverse_suggestions import reverse_retrieval_pool
from omop_core.models import SourceCodeConceptMapping, SourceVocabulary, SourceVocabularyTerm, SuggestRun, UmlsConcept, UmlsRelease, UmlsSourceCode
from omop_core.services.suggest_jobs import InlineDispatcher, use_dispatcher
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db
BASE = '/api/v1/concept-to-code/'


@pytest.fixture
def client(django_user_model):
    client = APIClient()
    client.force_authenticate(django_user_model.objects.create_user(email='catalog-curator@test.com', is_staff=True))
    return client


@pytest.fixture
def albumin():
    return ConceptFactory(concept_id=3024561, concept_code='1751-7',
                          concept_name='Albumin [Mass/volume] in Serum or Plasma')


def vocabulary_source(**overrides):
    return ConceptFactory(vocabulary_id='SNOMED', concept_code='ALBUMIN-SOURCE',
                          concept_name='Albumin measurement, serum', **overrides)


@pytest.fixture
def snomed_vocabulary():
    from tests.factories import VocabularyFactory
    return VocabularyFactory(vocabulary_id='SNOMED')


def preview(client, concept, **overrides):
    with use_dispatcher(InlineDispatcher()):
        response = client.post(BASE + 'suggest/', {
            'concept_ids': [concept.pk], 'strategies': ['lexical'],
            'include_zero_seen': True, 'limit': 25, **overrides,
        }, format='json')
    assert response.status_code == 202, response.data
    assert response.data['state'] == 'success', response.data
    return response.data


def propose(client, concept, run, candidate, **overrides):
    return client.post(f'{BASE}{concept.pk}/mappings/', {
        'run_id': run['run_id'], 'source_code': candidate['source_code'],
        'source_vocabulary_id': candidate['source_vocabulary_id'], **overrides,
    }, format='json')


def test_albumin_finds_vocabulary_sources_when_all_existing_mappings_are_approved(client, albumin, snomed_vocabulary):
    for code in ('1751-7', 'albumin', 'serum albumin'):
        SourceCodeConceptMapping.objects.create(source_code=code, target_concept=albumin,
                                                source_code_description='Albumin', status='approved')
    source = vocabulary_source()
    with patch('omop_core.mapping.suggestions.rank_candidates_dispatch', return_value=(None, 'Review specimen and method', [], {})) as rank:
        run = preview(client, albumin, ranking_model='anthropic')
    candidates = run['activity'][0]['candidates']
    assert len(candidates) == 1
    assert candidates[0]['source_code'] == source.concept_code
    assert candidates[0]['mapping_id'] is None
    assert candidates[0]['status'] == 'unmapped'
    rank.assert_called_once()
    assert rank.call_args.args[1][0]['concept_id'] == albumin.pk
    assert SourceCodeConceptMapping.objects.count() == 3
    assert not SourceCodeConceptMapping.objects.exclude(status='approved').exists()


def test_age_can_find_a_vocabulary_candidate_with_an_empty_mapping_registry(client, snomed_vocabulary):
    from tests.factories import DomainFactory
    DomainFactory(domain_id='Observation')
    age = ConceptFactory(concept_id=3022304, concept_code='30525-0', concept_name='Age', domain_id='Observation')
    ConceptFactory(vocabulary_id='SNOMED', concept_code='AGE-SOURCE', concept_name='Age', domain_id='Observation')
    run = preview(client, age)
    assert [c['source_code'] for c in run['activity'][0]['candidates']] == ['AGE-SOURCE']
    assert not SourceCodeConceptMapping.objects.exists()
    assert reverse_retrieval_pool(age, strategies=['lexical'], limit=25) == []


def test_catalog_reuses_an_editable_mapping_without_overwriting_its_source_description(albumin, snomed_vocabulary):
    source = vocabulary_source()
    row = SourceCodeConceptMapping.objects.create(source_code=source.concept_code, source_vocabulary_id='SNOMED',
                                                   source_code_description='', occurrence_count=9)
    candidates = reverse_retrieval_pool(albumin, strategies=['lexical'], include_zero_seen=True, limit=25)
    assert len(candidates) == 1
    assert candidates[0]['mapping_id'] == row.pk
    assert candidates[0]['source_code_description'] == ''
    assert candidates[0]['occurrence_count'] == 9


@pytest.mark.parametrize('decision', ['approved', 'rejected', 'athena', 'linked', 'wrong-domain', 'oid-approved'])
def test_catalog_does_not_bypass_existing_source_decisions(albumin, snomed_vocabulary, decision):
    source = vocabulary_source()
    SourceCodeConceptMapping.objects.create(
        source_code=source.concept_code,
        source_vocabulary_id='urn:oid:2.16.840.1.113883.6.96' if decision == 'oid-approved' else 'SNOMED',
        status='approved' if decision == 'oid-approved' else decision if decision in ('approved', 'rejected') else 'proposed',
        target_concept=albumin if decision == 'linked' else None,
        origin_system='athena' if decision == 'athena' else '',
        domain_id='Drug' if decision == 'wrong-domain' else 'Measurement',
    )
    assert reverse_retrieval_pool(albumin, strategies=['lexical'], include_zero_seen=True, limit=25) == []


def publisher_term(code='ALB', **overrides):
    vocabulary, _ = SourceVocabulary.objects.get_or_create(vocabulary_id='NCIt', defaults={
        'name': 'NCI Thesaurus', 'release_version': 'test', 'source_url': 'https://example.test',
        'archive_sha256': '0' * 64, 'loaded_at': timezone.now(),
    })
    return SourceVocabularyTerm.objects.create(vocabulary=vocabulary, code=code, name='Albumin measurement',
                                               search_text='ALBUMIN MEASUREMENT', **overrides)


def test_publisher_terms_and_umls_codes_are_candidates_without_existing_mappings(albumin):
    term = publisher_term()
    publisher_term('RETIRED', retired=True)
    release = UmlsRelease.objects.create(release_version='test')
    cui = UmlsConcept.objects.create(cui='C0000001', release=release)
    UmlsSourceCode.objects.create(concept=cui, root_source='LNC', code='1751-7', term_type='PT', name='Albumin')
    UmlsSourceCode.objects.create(concept=cui, root_source='CPT', code='82040', term_type='PT', name='Albumin; serum', is_preferred=True)
    UmlsSourceCode.objects.create(concept=cui, root_source='CPT', code='82040', term_type='SY', name='Albumin test')
    candidates = reverse_retrieval_pool(albumin, strategies=['umls', 'lexical'], include_zero_seen=True, limit=25)
    assert {(c['source_vocabulary_id'], c['source_code']) for c in candidates} == {('NCIt', term.code), ('CPT4', '82040')}
    assert all(c['mapping_id'] is None for c in candidates)
    assert not SourceCodeConceptMapping.objects.exists()


def test_other_catalog_representations_cannot_bypass_retired_or_incompatible_omop_metadata(albumin, snomed_vocabulary):
    from tests.factories import DomainFactory, VocabularyFactory
    DomainFactory(domain_id='Drug')
    VocabularyFactory(vocabulary_id='NCIt')
    vocabulary_source(invalid_reason='D')
    term = publisher_term()
    ConceptFactory(vocabulary_id='NCIt', concept_code=term.code, concept_name=term.name, domain_id='Drug')
    assert reverse_retrieval_pool(albumin, strategies=['lexical'], include_zero_seen=True, limit=25) == []


def test_a_new_source_must_be_proposed_before_a_separate_guarded_approval(client, albumin, snomed_vocabulary):
    source = vocabulary_source()
    run = preview(client, albumin)
    candidate = run['activity'][0]['candidates'][0]
    assert propose(client, albumin, run, candidate, status='approved').status_code == 400
    assert not SourceCodeConceptMapping.objects.exists()
    with patch('patient_portal.api.views.repoint_clinical_rows') as repoint:
        response = propose(client, albumin, run, candidate)
    assert response.status_code == 201, response.data
    repoint.assert_not_called()
    row = SourceCodeConceptMapping.objects.get(pk=response.data['mapping_id'])
    assert row.status == 'proposed' and row.target_concept_id == albumin.pk
    assert row.source_concept_id == source.pk
    with patch('patient_portal.api.views.repoint_clinical_rows', return_value={'rows_updated': 0, 'person_ids': set()}):
        approved = client.post(f'{BASE}{albumin.pk}/mappings/{row.pk}/', {
            'status': 'approved', 'expected_updated_at': response.data['updated_at'],
        }, format='json')
    assert approved.status_code == 200, approved.data
    row.refresh_from_db()
    assert row.status == 'approved' and row.reviewer_id is not None
    assert propose(client, albumin, run, candidate).status_code == 409
    assert SourceCodeConceptMapping.objects.count() == 1


@pytest.mark.parametrize('change', ['name', 'retired', 'new-mapping'])
def test_changed_sources_and_concurrent_mapping_creation_reject_stale_previews(client, albumin, snomed_vocabulary, change):
    source = vocabulary_source()
    run = preview(client, albumin)
    candidate = run['activity'][0]['candidates'][0]
    if change == 'name':
        source.concept_name = 'Corrected meaning'; source.save()
    elif change == 'retired':
        source.invalid_reason = 'D'; source.save()
    else:
        SourceCodeConceptMapping.objects.create(source_vocabulary_id='SNOMED', source_code=source.concept_code, status='rejected')
    assert propose(client, albumin, run, candidate).status_code == 409
    assert not SourceCodeConceptMapping.objects.filter(target_concept=albumin).exists()


def test_a_proposal_requires_the_exact_source_in_a_reverse_preview(client, albumin, snomed_vocabulary):
    vocabulary_source()
    run = preview(client, albumin)
    candidate = run['activity'][0]['candidates'][0]
    assert propose(client, albumin, run, candidate, source_code='FORGED').status_code == 400
    assert propose(client, albumin, run, candidate, run_id='invalid').status_code == 400
    forward = SuggestRun.objects.create(direction='forward', state='success')
    assert propose(client, albumin, run, candidate, run_id=str(forward.pk)).status_code == 404
    assert client.post(f'{BASE}{albumin.pk}/mappings/', [], format='json').status_code == 400
    client.force_authenticate(None)
    assert propose(client, albumin, run, candidate).status_code in (401, 403)
    assert not SourceCodeConceptMapping.objects.exists()


def test_publisher_proposals_preserve_full_long_labels_in_notes(client, albumin):
    term = publisher_term()
    term.name = 'Albumin ' + 'source terminology label ' * 20
    term.save()
    run = preview(client, albumin)
    response = propose(client, albumin, run, run['activity'][0]['candidates'][0])
    assert response.status_code == 201, response.data
    row = SourceCodeConceptMapping.objects.get()
    assert len(row.source_code_description) == 255
    assert term.name.strip() in row.notes


def test_an_oid_decision_blocks_catalog_reuse_of_a_canonical_proposal(albumin, snomed_vocabulary):
    source = vocabulary_source()
    for vocabulary, status in [('SNOMED', 'proposed'), ('urn:oid:2.16.840.1.113883.6.96', 'approved')]:
        SourceCodeConceptMapping.objects.create(source_code=source.concept_code,
                                                source_vocabulary_id=vocabulary, status=status)
    assert reverse_retrieval_pool(albumin, strategies=['lexical'], include_zero_seen=True, limit=25) == []


def test_publisher_revision_and_professional_access_are_rechecked(client, albumin, django_user_model):
    term = publisher_term()
    run = preview(client, albumin)
    candidate = run['activity'][0]['candidates'][0]
    client.force_authenticate(django_user_model.objects.create_user(email='catalog-patient@test.com'))
    assert propose(client, albumin, run, candidate).status_code == 403
    # A professional who can propose still cannot create-and-approve.
    with patch('patient_portal.api.views._can_manage_field_mappings', return_value=True):
        assert propose(client, albumin, run, candidate, status='approved').status_code == 400
        term.vocabulary.release_version = 'replacement-release'
        term.vocabulary.save()
        assert propose(client, albumin, run, candidate).status_code == 409
    assert not SourceCodeConceptMapping.objects.exists()


def test_catalog_query_count_does_not_grow_per_candidate(albumin):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    publisher_term()
    with CaptureQueriesContext(connection) as single:
        first = reverse_retrieval_pool(albumin, strategies=['lexical'], include_zero_seen=True, limit=25)
    for index in range(10):
        publisher_term(f'ALB{index}')
    with CaptureQueriesContext(connection) as many:
        candidates = reverse_retrieval_pool(albumin, strategies=['lexical'], include_zero_seen=True, limit=25)
    assert len(first) == 1 and len(candidates) == 11
    assert len(single) == len(many)
