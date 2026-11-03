"""Curator priority on Concept to Code (#1914): ranking, the list, copying, Suggest."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from omop_core.mapping import suggestions as suggest
from omop_core.mapping.suggestion_context import candidate_context
from omop_core.models import ConceptMappingPriority, FieldConceptMapping
from omop_core.services import concept_priority
from omop_core.services.reference_transfer import apply_reference, read_reference
from tests.factories import ConceptFactory

pytestmark = pytest.mark.django_db
BASE = '/api/v1/concept-to-code/'
PRIORITY = f'{BASE}priority/'


@pytest.fixture
def client(django_user_model):
    instance = APIClient()
    instance.user = django_user_model.objects.create_user(email='priority-curator@test.com', is_staff=True)
    instance.force_authenticate(instance.user)
    return instance


def field_concepts(*names):
    concepts = []
    for index, name in enumerate(names):
        concept = ConceptFactory(concept_name=name, concept_code=f'P{index}')
        FieldConceptMapping.objects.create(concept=concept, field_name=f'field_{index}', omop_table='measurement')
        concepts.append(concept)
    return concepts


def ranked():
    return list(ConceptMappingPriority.objects.order_by('position').values_list('concept_id', 'position'))


# ── Moving ──────────────────────────────────────────────────────────────────

def test_move_inserts_reorders_clamps_and_unranks_keeping_positions_dense():
    a, b, c = field_concepts('Alpha', 'Beta', 'Gamma')
    assert concept_priority.move(a, 1) == 1
    assert concept_priority.move(b, 1) == 1
    assert ranked() == [(b.pk, 1), (a.pk, 2)]
    # Past the end clamps to the end.
    assert concept_priority.move(c, 99) == 3
    # Moving an already ranked concept does not duplicate it.
    assert concept_priority.move(c, 1) == 1
    assert ranked() == [(c.pk, 1), (b.pk, 2), (a.pk, 3)]
    assert concept_priority.move(b, None) is None
    assert ranked() == [(c.pk, 1), (a.pk, 2)]


def test_priority_endpoint_ranks_a_concept_and_records_who(client):
    a, b = field_concepts('Alpha', 'Beta')
    assert client.post(PRIORITY, {'concept_id': a.pk, 'position': 1}, format='json').status_code == 200

    response = client.post(PRIORITY, {'concept_id': b.pk, 'position': 1}, format='json')

    assert response.status_code == 200
    assert response.data == {'concept_id': b.pk, 'position': 1, 'prioritized': 2}
    assert ConceptMappingPriority.objects.get(concept=b).updated_by == client.user
    assert client.post(PRIORITY, {'concept_id': b.pk, 'position': None}, format='json').data['position'] is None
    assert ranked() == [(a.pk, 1)]


@pytest.mark.parametrize('payload', [
    {}, {'concept_id': 'x', 'position': 1}, {'concept_id': 1, 'position': 0},
    {'concept_id': 1, 'position': '2'}, {'concept_id': 1},
])
def test_priority_endpoint_validates(client, payload):
    field_concepts('Alpha')
    assert client.post(PRIORITY, payload, format='json').status_code == 400


def test_priority_endpoint_rejects_nonstandard_concepts_and_non_curators(client, django_user_model):
    nonstandard = ConceptFactory(standard_concept=None)
    assert client.post(PRIORITY, {'concept_id': nonstandard.pk, 'position': 1}, format='json').status_code == 404
    (concept,) = field_concepts('Alpha')
    patient = APIClient()
    patient.force_authenticate(django_user_model.objects.create_user(email='patient@test.com'))
    assert patient.post(PRIORITY, {'concept_id': concept.pk, 'position': 1}, format='json').status_code == 403
    assert APIClient().post(PRIORITY, {'concept_id': concept.pk, 'position': 1}, format='json').status_code in (401, 403)
    assert not ConceptMappingPriority.objects.exists()


# ── The list ────────────────────────────────────────────────────────────────

def test_list_shows_ranked_concepts_first_then_alphabetical_under_filters(client):
    alpha, beta, gamma, delta = field_concepts('Alpha', 'Beta', 'Gamma serum', 'Delta serum')
    concept_priority.move(gamma, 1)
    concept_priority.move(beta, 2)

    results = client.get(BASE).data['results']
    assert [r['concept_id'] for r in results] == [gamma.pk, beta.pk, alpha.pk, delta.pk]
    assert [r['priority'] for r in results] == [1, 2, None, None]
    # A filter keeps the global order of what it shows.
    assert [r['concept_id'] for r in client.get(BASE, {'search': 'serum'}).data['results']] == [gamma.pk, delta.pk]
    ranked_only = client.get(BASE, {'prioritized': '1'}).data
    assert [r['concept_id'] for r in ranked_only['results']] == [gamma.pk, beta.pk]
    assert ranked_only['prioritized'] == 2


def test_list_page_size_brings_later_concepts_onto_one_page(client):
    field_concepts(*[f'Concept {index:03d}' for index in range(60)])
    assert len(client.get(BASE).data['results']) == 50
    page = client.get(BASE, {'page_size': 100}).data
    assert (len(page['results']), page['page_size'], page['total']) == (60, 100, 60)
    assert client.get(BASE, {'page_size': 7}).status_code == 400
    assert client.get(BASE, {'page_size': 'all'}).status_code == 400


# ── Copying between instances ───────────────────────────────────────────────

def test_copy_reference_data_carries_priority_by_concept_key():
    a, b = field_concepts('Alpha', 'Beta')
    concept_priority.move(a, 1)
    concept_priority.move(b, 2)
    payload = read_reference('default')
    ConceptMappingPriority.objects.all().delete()

    stats = apply_reference(payload)

    assert ranked() == [(a.pk, 1), (b.pk, 2)]
    assert stats.created['ConceptMappingPriority'] == 2
    assert ConceptMappingPriority.objects.filter(updated_by__isnull=False).count() == 0


# ── Suggest ─────────────────────────────────────────────────────────────────

def _lexical_hit(concept):
    return {'concept_id': concept.pk, 'concept_name': concept.concept_name, 'concept_code': concept.concept_code,
            'vocabulary_id': concept.vocabulary_id, 'concept_class_id': concept.concept_class_id,
            'domain_id': concept.domain_id, 'standard_concept': 'S', 'lexical_score': 0.9, 'retrieval': 'lexical'}


def test_retrieval_keeps_a_prioritized_concept_lexical_search_cut(monkeypatch):
    winner = ConceptFactory(concept_name='Glucose measurement', domain_id='Measurement')
    prioritized = ConceptFactory(concept_name='Serum glucose', domain_id='Measurement')
    unrelated = ConceptFactory(concept_name='Hemoglobin A1c', domain_id='Measurement')
    concept_priority.move(prioritized, 1)
    concept_priority.move(unrelated, 2)
    # The best-N cut kept only the other concept.
    monkeypatch.setattr(suggest, 'lexical_candidates', lambda *args, **kwargs: [_lexical_hit(winner)])

    candidates, _, _ = suggest.retrieval_pool(
        source_code='GLU', source_vocabulary_id='Local', source_text='Serum glucose level',
        domain_id='Measurement', strategies=['lexical'])

    by_id = {c['concept_id']: c for c in candidates}
    assert set(by_id) == {winner.pk, prioritized.pk}  # never the unmatched one
    assert by_id[prioritized.pk]['curator_priority'] == 1
    assert by_id[prioritized.pk]['priority_retained'] is True
    assert 'curator_priority' not in by_id[winner.pk]


def test_retrieval_marks_prioritized_candidates_and_leaves_others_alone(monkeypatch):
    found = ConceptFactory(concept_name='Serum glucose', domain_id='Measurement')
    concept_priority.move(found, 3)
    monkeypatch.setattr(suggest, 'lexical_candidates', lambda *args, **kwargs: [_lexical_hit(found)])

    candidates, _, _ = suggest.retrieval_pool(
        source_code='GLU', source_vocabulary_id='Local', source_text='Serum glucose',
        domain_id='Measurement', strategies=['lexical'])

    assert [(c['concept_id'], c['curator_priority']) for c in candidates] == [(found.pk, 1)]
    assert 'priority_retained' not in candidates[0]


def test_ranker_sees_priority_as_a_tie_breaker_and_the_note_says_so():
    concept = ConceptFactory(concept_name='Serum glucose')
    candidate = {**_lexical_hit(concept), 'curator_priority': 2}
    assert candidate_context(candidate)['curator_priority'] == 2
    assert 'curator_priority' not in candidate_context(_lexical_hit(concept))
    assert 'equally good clinical matches' in suggest._RANKING_SYSTEM

    with patch.object(suggest, 'rank_candidates', return_value=(candidate, 'high confidence: same analyte', [])):
        chosen, note, _, _ = suggest.rank_candidates_dispatch('GLU', [candidate], ranking_model='anthropic')

    assert chosen is candidate
    assert note == 'high confidence: same analyte Curator priority 2.'


def test_jev_criteria_tag_prioritized_candidates(settings):
    from tests.test_jev_ranking import _mock_jev_response
    settings.JEV_API_KEY = 'test-key'
    first, second = ConceptFactory(concept_name='Serum glucose'), ConceptFactory(concept_name='Glucose')
    candidates = [{**_lexical_hit(first), 'curator_priority': 1}, _lexical_hit(second)]
    patcher, mock_post = _mock_jev_response(str(first.pk), {str(first.pk): 0.6, str(second.pk): 0.4})

    with patcher:
        suggest.rank_candidates_jev('GLU', candidates, 'Serum glucose')

    question = mock_post.call_args.kwargs['json']['questions']['best_match']
    assert question['criteria'][str(first.pk)].endswith('[curator priority 1]')
    assert 'curator priority' not in question['criteria'][str(second.pk)]
    assert 'never over a better one' in question['instructions']
