"""Tests for Jev ranking integration and ranking model dispatch."""
import pytest
from unittest.mock import patch, MagicMock

from omop_core.mapping.suggestions import (
    DEFAULT_RANKING_MODEL,
    RANKING_MODELS,
    rank_candidates_dispatch,
    rank_candidates_jev,
)


def _candidates():
    return [
        {
            'concept_id': 100,
            'concept_name': 'Hemoglobin A1c',
            'concept_code': '4548-4',
            'vocabulary_id': 'LOINC',
            'lexical_score': 0.85,
            'retrieval': 'lexical',
        },
        {
            'concept_id': 200,
            'concept_name': 'Glucose',
            'concept_code': '2345-7',
            'vocabulary_id': 'LOINC',
            'lexical_score': 0.70,
            'retrieval': 'lexical',
        },
    ]


class TestRankingModelConstants:
    def test_default_is_anthropic(self):
        assert DEFAULT_RANKING_MODEL == 'anthropic'

    def test_both_models_in_set(self):
        assert RANKING_MODELS == {'anthropic', 'jev', 'both'}


class TestRankCandidatesDispatch:
    def test_routes_to_anthropic_by_default(self):
        with patch('omop_core.mapping.suggestions.rank_candidates') as mock_rank:
            mock_rank.return_value = (_candidates()[0], 'high confidence: test', [{'concept_id': 100, 'concept_name': 'HbA1c', 'confidence': 0.85, 'ranker': 'anthropic'}])
            chosen, note, alts, timings = rank_candidates_dispatch(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
            )
            mock_rank.assert_called_once()
            assert chosen['concept_id'] == 100
            assert alts is not None
            assert 'anthropic_ms' in timings

    def test_routes_to_jev_when_requested(self):
        with patch('omop_core.mapping.suggestions.rank_candidates_jev') as mock_jev:
            mock_jev.return_value = (_candidates()[0], 'high confidence (Jev 85%)', [])
            chosen, note, alts, timings = rank_candidates_dispatch(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
                ranking_model='jev',
            )
            mock_jev.assert_called_once()
            assert chosen['concept_id'] == 100
            assert 'jev_ms' in timings

    def test_explicit_anthropic_routes_correctly(self):
        with patch('omop_core.mapping.suggestions.rank_candidates') as mock_rank:
            mock_rank.return_value = (_candidates()[0], 'test note', None)
            chosen, note, alts, timings = rank_candidates_dispatch(
                'HbA1c', _candidates(), ranking_model='anthropic',
            )
            mock_rank.assert_called_once()
            assert 'anthropic_ms' in timings

    def test_both_runs_both_rankers_concurrently(self):
        a_alts = [{'concept_id': 100, 'concept_name': 'HbA1c', 'confidence': 0.55, 'ranker': 'anthropic'}]
        j_alts = [{'concept_id': 200, 'concept_name': 'Glycated', 'confidence': 0.90, 'ranker': 'jev'}]
        with patch('omop_core.mapping.suggestions.rank_candidates') as mock_a, \
             patch('omop_core.mapping.suggestions.rank_candidates_jev') as mock_j:
            mock_a.return_value = (_candidates()[0], 'medium confidence: ok', a_alts)
            mock_j.return_value = (_candidates()[1], 'high confidence (Jev 90%)', j_alts)
            chosen, note, alts, timings = rank_candidates_dispatch(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
                ranking_model='both',
            )
            mock_a.assert_called_once()
            mock_j.assert_called_once()
            # Jev wins because 0.90 > 0.55
            assert chosen['concept_id'] == 200
            assert 'Jev wins' in note
            # Both rankers' alternatives are merged
            assert len(alts) == 2
            # Timing for both rankers
            assert 'anthropic_ms' in timings
            assert 'jev_ms' in timings


def _mock_jev_response(choice, probabilities, *, raise_exc=None):
    """Return a patcher for requests.post that returns a Jev API response."""
    json_response = {
        'model': 'jev-1.13.0',
        'answers': {
            'best_match': {
                'type': 'choice',
                'choice': choice,
                'confidence': max(probabilities.values()) if probabilities else 0,
                'probabilities': probabilities,
            },
        },
        'usage': {'input_tokens': 100, 'output_tokens': 20},
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = json_response
    mock_resp.raise_for_status = MagicMock()
    mock_post = MagicMock(return_value=mock_resp)
    if raise_exc:
        mock_post.side_effect = raise_exc
    return patch('requests.post', mock_post), mock_post


class TestRankCandidatesJev:
    def test_missing_key_degrades_to_fallback(self, settings):
        settings.JEV_API_KEY = ''
        chosen, note, alts = rank_candidates_jev(
            'HbA1c', _candidates(), 'Hemoglobin A1c',
        )
        # Falls back to first candidate.
        assert chosen is not None
        assert chosen['concept_id'] == 100
        assert 'JEV_API_KEY is not configured' in note
        assert alts is None

    def test_missing_key_with_require_model_returns_none(self, settings):
        settings.JEV_API_KEY = ''
        chosen, note, alts = rank_candidates_jev(
            'HbA1c', _candidates(), 'Hemoglobin A1c',
            require_model_selection=True,
        )
        assert chosen is None
        assert 'JEV_API_KEY is not configured' in note

    def test_empty_candidates(self):
        chosen, note, alts = rank_candidates_jev('HbA1c', [], 'Hemoglobin A1c')
        assert chosen is None
        assert 'No candidate concept' in note
        assert alts is None

    def test_successful_jev_response(self, settings):
        settings.JEV_API_KEY = 'test-key'
        patcher, mock_post = _mock_jev_response('100', {'100': 0.85, '200': 0.15})
        with patcher:
            chosen, note, alts = rank_candidates_jev(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
            )
        assert chosen is not None
        assert chosen['concept_id'] == 100
        assert 'Jev 85%' in note
        assert len(alts) == 2
        assert alts[0]['confidence'] == 0.85
        assert alts[0]['concept_id'] == 100
        mock_post.assert_called_once()
        # Verify the payload uses the correct API format.
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]['json'] if 'json' in call_kwargs[1] else call_kwargs[0][1] if len(call_kwargs[0]) > 1 else None
        if payload is None:
            payload = call_kwargs.kwargs.get('json')
        assert 'model' in payload
        assert 'questions' in payload
        assert 'best_match' in payload['questions']
        assert 'criteria' in payload['questions']['best_match']

    def test_jev_declines_all_candidates(self, settings):
        settings.JEV_API_KEY = 'test-key'
        patcher, _ = _mock_jev_response('none', {'100': 0.3, '200': 0.2})
        with patcher:
            chosen, note, alts = rank_candidates_jev(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
            )
        assert chosen is None
        assert 'No suitable concept (Jev ranking)' in note
        assert alts is not None
        assert len(alts) == 2

    def test_alternatives_sorted_by_confidence(self, settings):
        settings.JEV_API_KEY = 'test-key'
        patcher, _ = _mock_jev_response('200', {'100': 0.3, '200': 0.7})
        with patcher:
            chosen, note, alts = rank_candidates_jev(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
            )
        assert chosen['concept_id'] == 200
        # Alternatives sorted descending by confidence.
        assert alts[0]['confidence'] > alts[1]['confidence']

    def test_api_failure_degrades(self, settings):
        settings.JEV_API_KEY = 'test-key'
        patcher, _ = _mock_jev_response('100', {}, raise_exc=Exception('Connection refused'))
        with patcher:
            chosen, note, alts = rank_candidates_jev(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
            )
        # Falls back to first candidate.
        assert chosen is not None
        assert chosen['concept_id'] == 100
        assert 'Jev ranking request failed' in note
