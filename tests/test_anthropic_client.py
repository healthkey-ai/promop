"""The Anthropic client names a workspace when one is configured."""
from types import SimpleNamespace
from unittest.mock import patch

from omop_core.mapping import suggestions
from omop_core.mapping.anthropic_client import anthropic_client


def test_workspace_header_is_sent_when_configured(settings):
    settings.ANTHROPIC_API_KEY = 'k'
    settings.ANTHROPIC_WORKSPACE_ID = 'wrkspc_123'
    with patch('anthropic.Anthropic') as client:
        anthropic_client()
    client.assert_called_once_with(api_key='k', default_headers={'anthropic-workspace-id': 'wrkspc_123'})


def test_no_workspace_header_by_default(settings):
    settings.ANTHROPIC_API_KEY = 'k'
    settings.ANTHROPIC_WORKSPACE_ID = ''
    with patch('anthropic.Anthropic') as client:
        anthropic_client()
    client.assert_called_once_with(api_key='k', default_headers=None)


def test_unscoped_key_is_reported_as_workspace_required(settings):
    settings.ANTHROPIC_API_KEY = 'k'
    settings.ANTHROPIC_WORKSPACE_ID = ''

    class BadRequest(Exception):
        status_code = 400
        body = {'type': 'error', 'error': {'type': 'invalid_request_error', 'message': (
            'This API key is not scoped to a workspace, so this request must include the '
            'anthropic-workspace-id header with the ID of the workspace to use.')}}

    failing = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(BadRequest())))
    candidates = [{'concept_id': 1, 'concept_name': 'A', 'concept_code': '1', 'vocabulary_id': 'LOINC',
                   'domain_id': 'Measurement', 'concept_class_id': 'Lab Test', 'lexical_score': 0.5}]
    with patch('anthropic.Anthropic', return_value=failing):
        chosen, note, alternatives = suggestions.rank_candidates('X', candidates, 'x', require_model_selection=True)
    assert chosen is None and alternatives is None
    assert 'not scoped to a workspace. Set ANTHROPIC_WORKSPACE_ID' in note
