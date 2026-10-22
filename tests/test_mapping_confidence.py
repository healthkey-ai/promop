"""Suggestion confidence on source-code mappings (#1709)."""
import importlib
from unittest.mock import patch

import pytest

from omop_core.mapping import suggestions
from omop_core.mapping.suggestions import chosen_confidence
from omop_core.models import SourceCodeConceptMapping
from tests.test_curator_edits_are_not_suggestions import (  # noqa: F401 - fixtures
    admin_client, as_candidate, concepts, suggested_row,
)
from tests.test_reverse_catalog import (  # noqa: F401 - fixtures
    albumin, client, preview, propose, snomed_vocabulary, vocabulary_source,
)

pytestmark = pytest.mark.django_db
BASE = '/api/v1/concept-to-code/'


def test_chosen_confidence_takes_the_first_score_for_the_winner():
    chosen = {'concept_id': 1}
    alternatives = [
        {'concept_id': 2, 'confidence': 0.99},
        {'concept_id': 1, 'confidence': 0.55, 'ranker': 'anthropic'},
        {'concept_id': 1, 'confidence': 0.72, 'ranker': 'jev'},
    ]
    # Dual ranking lists the winning ranker first; a later score is the loser's.
    assert chosen_confidence(chosen, alternatives) == 0.55
    assert chosen_confidence(chosen, [{'concept_id': 2, 'confidence': 0.9}]) is None
    assert chosen_confidence(None, alternatives) is None
    assert chosen_confidence(chosen, None) is None
    # A ranker stub that returns (chosen, note) shifts the timings dict into
    # this slot; that must read as unscored, not fail the run's write.
    assert chosen_confidence(chosen, {'anthropic_ms': 3}) is None


@pytest.mark.parametrize('note, expected', [
    ('high confidence: same analyte and specimen', 0.85),
    ('Search expanded with "albumin". medium confidence: broader concept', 0.55),
    ('low confidence: unclear', 0.25),
    ('high confidence (Jev 83%)', 0.83),
    ('Jev wins (91% vs Anthropic 55%). high confidence (Jev 91%)', 0.91),
    ('high confidence: reason (Anthropic 85% vs Jev 40%)', 0.85),
    ('No candidate concept found by any enabled strategy.', None),
    ('waiting on lab confirmation', None),
])
def test_backfill_reads_confidence_from_suggest_notes(note, expected):
    migration = importlib.import_module('omop_core.migrations.0278_sccm_suggestion_confidence')
    assert migration.note_confidence(note) == expected


def test_forward_suggest_stores_the_winners_confidence(monkeypatch, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick)
    monkeypatch.setattr(suggestions, 'umls_candidates', lambda *a: ([as_candidate(curator_pick)], 'C1'))
    monkeypatch.setattr(suggestions, 'rank_candidates', lambda source_value, candidates, *a, **kw: (
        candidates[0], 'high confidence: match',
        [{'concept_id': candidates[0]['concept_id'], 'confidence': 0.85, 'ranker': 'anthropic'}],
    ))
    suggestions.suggest_mappings('condition', strategies=['umls'], min_occurrences=1, resuggest=True)
    row.refresh_from_db()
    assert row.target_concept_id == curator_pick.pk
    assert row.suggestion_confidence == 0.85


def test_a_curator_moving_the_destination_clears_the_confidence(admin_client, concepts):
    model_pick, curator_pick = concepts
    row = suggested_row(model_pick, suggestion_confidence=0.85)
    response = admin_client.patch(f'/api/v1/code-mappings/{row.pk}/', {'notes': 'checked'}, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db()
    assert row.suggestion_confidence == 0.85, 'an edit that keeps the destination keeps it'
    response = admin_client.patch(f'/api/v1/code-mappings/{row.pk}/',
                                  {'destination_concept_id': curator_pick.pk}, format='json')
    assert response.status_code == 200, response.data
    row.refresh_from_db()
    assert row.suggestion_confidence is None


def test_reverse_proposal_keeps_the_ranked_confidence(client, albumin, snomed_vocabulary):
    vocabulary_source()
    ranked = ({'concept_id': albumin.pk}, 'high confidence: same analyte',
              [{'concept_id': albumin.pk, 'confidence': 0.85, 'ranker': 'anthropic'}], {})
    with patch('omop_core.mapping.suggestions.rank_candidates_dispatch', return_value=ranked):
        run = preview(client, albumin, ranking_model='anthropic')
    candidate = run['activity'][0]['candidates'][0]
    assert candidate['confidence'] == 0.85
    response = propose(client, albumin, run, candidate)
    assert response.status_code == 201, response.data
    assert response.data['confidence'] == 0.85
    assert SourceCodeConceptMapping.objects.get(pk=response.data['mapping_id']).suggestion_confidence == 0.85


def test_unranked_reverse_proposal_has_no_confidence(client, albumin, snomed_vocabulary):
    vocabulary_source()
    run = preview(client, albumin)
    candidate = run['activity'][0]['candidates'][0]
    assert candidate.get('confidence') is None
    response = propose(client, albumin, run, candidate)
    assert response.status_code == 201, response.data
    assert response.data['confidence'] is None


def test_detail_orders_by_confidence_with_unscored_rows_last(client, albumin):
    def row(code, confidence, seen):
        return SourceCodeConceptMapping.objects.create(
            source_code=code, source_vocabulary_id='LOCAL', source_code_description=code,
            domain_id='Measurement', omop_table='measurement', target_concept=albumin,
            occurrence_count=seen, suggestion_confidence=confidence,
        )
    row('LOW', 0.25, 50), row('NONE', None, 99), row('HIGH', 0.85, 1), row('MID', 0.55, 5)
    url = f'{BASE}{albumin.pk}/'
    codes = lambda **params: [r['source_code'] for r in client.get(url, params).data['results']]  # noqa: E731
    assert codes() == ['NONE', 'LOW', 'MID', 'HIGH']
    assert codes(order='-confidence') == ['HIGH', 'MID', 'LOW', 'NONE']
    assert codes(order='confidence') == ['LOW', 'MID', 'HIGH', 'NONE']
    assert client.get(url, {'order': 'occurrence_count'}).status_code == 400
    assert client.get(url).data['results'][0]['confidence'] is None


def test_dual_ranking_stores_the_winning_rankers_score(monkeypatch):
    """A declining Jev still scores candidates; that is not the winner's score."""
    chosen = {'concept_id': 7, 'concept_name': 'X'}
    monkeypatch.setattr(suggestions, 'rank_candidates', lambda *a, **kw: (
        chosen, 'low confidence: weak', [{'concept_id': 7, 'confidence': 0.25, 'ranker': 'anthropic'}]))
    monkeypatch.setattr(suggestions, 'rank_candidates_jev', lambda *a, **kw: (
        None, 'No suitable concept (Jev ranking).', [{'concept_id': 7, 'confidence': 0.6, 'ranker': 'jev'}]))
    winner, _note, alternatives, _timings = suggestions.rank_candidates_dispatch(
        'X1', [chosen], 'X', ranking_model='both')
    assert winner == chosen
    assert chosen_confidence(winner, alternatives) == 0.25

    monkeypatch.setattr(suggestions, 'rank_candidates_jev', lambda *a, **kw: (
        chosen, 'high confidence (Jev 90%)', [{'concept_id': 7, 'confidence': 0.9, 'ranker': 'jev'}]))
    winner, _note, alternatives, _timings = suggestions.rank_candidates_dispatch(
        'X1', [chosen], 'X', ranking_model='both')
    assert chosen_confidence(winner, alternatives) == 0.9


def test_an_upload_moving_the_destination_clears_the_confidence(admin_client, concepts):
    from django.core.files.uploadedfile import SimpleUploadedFile
    model_pick, curator_pick = concepts
    moved = suggested_row(model_pick, source_code='MOVED', suggestion_confidence=0.85)
    kept = suggested_row(model_pick, source_code='KEPT', suggestion_confidence=0.85)
    body = (f'source code,source description,seen count,destination concept ID\n'
            f'MOVED,Moved,5,{curator_pick.pk}\nKEPT,Kept,5,{model_pick.pk}\n')
    response = admin_client.post('/api/v1/code-mappings/upload/', {
        'file': SimpleUploadedFile('codes.csv', body.encode(), content_type='text/csv'),
        'source_vocabulary_id': 'ICD10',
    }, format='multipart')
    assert response.status_code == 201, response.data
    moved.refresh_from_db(); kept.refresh_from_db()
    assert moved.target_concept_id == curator_pick.pk and moved.suggestion_confidence is None
    assert kept.suggestion_confidence == 0.85
