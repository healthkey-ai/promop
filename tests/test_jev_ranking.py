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
        assert RANKING_MODELS == {'anthropic', 'jev'}


class TestRankCandidatesDispatch:
    def test_routes_to_anthropic_by_default(self):
        with patch('omop_core.mapping.suggestions.rank_candidates') as mock_rank:
            mock_rank.return_value = (_candidates()[0], 'high confidence: test')
            chosen, note, alts = rank_candidates_dispatch(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
            )
            mock_rank.assert_called_once()
            assert chosen['concept_id'] == 100
            assert alts is None

    def test_routes_to_jev_when_requested(self):
        with patch('omop_core.mapping.suggestions.rank_candidates_jev') as mock_jev:
            mock_jev.return_value = (_candidates()[0], 'high confidence (Jev 85%)', [])
            chosen, note, alts = rank_candidates_dispatch(
                'HbA1c', _candidates(), 'Hemoglobin A1c',
                ranking_model='jev',
            )
            mock_jev.assert_called_once()
            assert chosen['concept_id'] == 100

    def test_explicit_anthropic_routes_correctly(self):
        with patch('omop_core.mapping.suggestions.rank_candidates') as mock_rank:
            mock_rank.return_value = (_candidates()[0], 'test note')
            rank_candidates_dispatch(
                'HbA1c', _candidates(), ranking_model='anthropic',
            )
            mock_rank.assert_called_once()


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

    def test_successful_jev_response(self, settings, monkeypatch):
        settings.JEV_API_KEY = 'test-key'

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'choice': '100',
            'probabilities': {'100': 0.85, '200': 0.15},
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        monkeypatch.setitem(__import__('sys').modules, 'httpx', mock_httpx)

        chosen, note, alts = rank_candidates_jev(
            'HbA1c', _candidates(), 'Hemoglobin A1c',
        )
        assert chosen is not None
        assert chosen['concept_id'] == 100
        assert 'Jev 85%' in note
        assert len(alts) == 2
        assert alts[0]['confidence'] == 0.85
        assert alts[0]['concept_id'] == 100

    def test_jev_declines_all_candidates(self, settings, monkeypatch):
        settings.JEV_API_KEY = 'test-key'

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'choice': 'none',
            'probabilities': {'100': 0.3, '200': 0.2},
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        monkeypatch.setitem(__import__('sys').modules, 'httpx', mock_httpx)

        chosen, note, alts = rank_candidates_jev(
            'HbA1c', _candidates(), 'Hemoglobin A1c',
        )
        assert chosen is None
        assert 'No suitable concept (Jev ranking)' in note
        assert alts is not None
        assert len(alts) == 2

    def test_alternatives_sorted_by_confidence(self, settings, monkeypatch):
        settings.JEV_API_KEY = 'test-key'

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            'choice': '200',
            'probabilities': {'100': 0.3, '200': 0.7},
        }
        mock_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp

        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        monkeypatch.setitem(__import__('sys').modules, 'httpx', mock_httpx)

        chosen, note, alts = rank_candidates_jev(
            'HbA1c', _candidates(), 'Hemoglobin A1c',
        )
        assert chosen['concept_id'] == 200
        # Alternatives sorted descending by confidence.
        assert alts[0]['confidence'] > alts[1]['confidence']

    def test_api_failure_degrades(self, settings, monkeypatch):
        settings.JEV_API_KEY = 'test-key'

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = Exception('Connection refused')

        mock_httpx = MagicMock()
        mock_httpx.Client.return_value = mock_client
        monkeypatch.setitem(__import__('sys').modules, 'httpx', mock_httpx)

        chosen, note, alts = rank_candidates_jev(
            'HbA1c', _candidates(), 'Hemoglobin A1c',
        )
        # Falls back to first candidate.
        assert chosen is not None
        assert chosen['concept_id'] == 100
        assert 'Jev ranking request failed' in note
