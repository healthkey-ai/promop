"""Ranking outages identify their cause without exposing credentials or prompts."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from omop_core.mapping.suggestions import rank_candidates

CANDIDATE = {'concept_id': 1, 'concept_name': 'Candidate', 'vocabulary_id': 'LOINC',
             'concept_code': '123', 'concept_class_id': 'Lab Test', 'lexical_score': 0.8}


@pytest.fixture
def client(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = 'test-ranking-secret-never-log'
    mocked = Mock()
    monkeypatch.setattr('anthropic.Anthropic', lambda **kwargs: mocked)
    return mocked


def test_missing_key_is_visible_in_note_and_log(settings, caplog):
    settings.ANTHROPIC_API_KEY = ''
    chosen, note = rank_candidates('sensitive-source', [CANDIDATE])
    assert chosen == CANDIDATE
    assert 'ANTHROPIC_API_KEY is not configured in the process' in note
    assert 'reason=missing_api_key' in caplog.text
    assert 'key_configured=False' in caplog.text
    assert 'sensitive-source' not in caplog.text


@pytest.mark.parametrize(('status', 'reason'), [
    (400, 'invalid_request'), (401, 'authentication_failed'), (403, 'permission_denied'),
    (404, 'model_not_found'), (429, 'rate_limited'), (500, 'request_failed'),
    (529, 'provider_overloaded'),
])
def test_provider_error_is_classified_without_raw_exception(client, caplog, status, reason):
    error = RuntimeError('test-ranking-secret-never-log sensitive-source raw-provider-body')
    error.status_code = status
    error.request_id = 'req_diagnostic_123'
    client.messages.create.side_effect = error
    chosen, note = rank_candidates('sensitive-source', [CANDIDATE])
    assert chosen == CANDIDATE
    assert 'Ranking model unavailable' in note
    assert f'reason={reason}' in caplog.text
    assert f'status_code={status}' in caplog.text
    assert 'request_id=req_diagnostic_123' in caplog.text
    assert 'key_configured=True' in caplog.text
    for private in ('test-ranking-secret-never-log', 'sensitive-source', 'raw-provider-body'):
        assert private not in caplog.text + note


@pytest.mark.parametrize(('name', 'reason'), [('APITimeoutError', 'timeout'), ('APIConnectionError', 'connection_failed')])
def test_network_failures_are_distinguished(client, caplog, name, reason):
    client.messages.create.side_effect = type(name, (Exception,), {})('private error')
    rank_candidates('private source', [CANDIDATE])
    assert f'reason={reason}' in caplog.text


@pytest.mark.parametrize(('text', 'stop', 'reason'), [
    ('', 'max_tokens', 'output_truncated'), ('not json', 'end_turn', 'invalid_output'),
    ('{"concept_id": 999}', 'end_turn', 'candidate_outside_pool'),
])
def test_response_failures_include_stop_reason_and_request_id(client, caplog, text, stop, reason):
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type='text', text=text)], stop_reason=stop, _request_id='req_output',
    )
    chosen, note = rank_candidates('private source', [CANDIDATE])
    assert chosen == CANDIDATE
    assert 'Details:' in note
    assert f'reason={reason}' in caplog.text
    assert f'stop_reason={stop}' in caplog.text
    assert 'request_id=req_output' in caplog.text


@pytest.mark.parametrize('nested', [False, True])
def test_insufficient_credits_explains_billing_without_exposing_body(client, caplog, nested):
    import anthropic
    import httpx

    body = {'type': 'invalid_request_error', 'message':
            'Your credit balance is too low to access the Anthropic API. private-provider-data'}
    if nested:
        body = {'error': body}
    response = httpx.Response(400, request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))
    client.messages.create.side_effect = anthropic.BadRequestError('private-exception', response=response, body=body)
    chosen, note = rank_candidates('private-source', [CANDIDATE])
    assert chosen == CANDIDATE
    assert 'insufficient credits' in note
    assert 'Plans & Billing' in note
    assert 'reason=insufficient_credit' in caplog.text
    for private in ('private-provider-data', 'private-exception', 'private-source'):
        assert private not in note + caplog.text


def test_expanded_search_stays_unresolved_when_key_is_missing(settings):
    settings.ANTHROPIC_API_KEY = ''
    chosen, note = rank_candidates('source', [CANDIDATE], require_model_selection=True)
    assert chosen is None
    assert 'expanded search remains unresolved' in note
    assert 'not configured' in note


def test_success_keeps_the_model_verdict(client, caplog):
    client.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(
        type='text', text='{"concept_id": 1, "confidence": "high", "reason": "Matching concept."}',
    )])
    chosen, note = rank_candidates('source', [CANDIDATE])
    assert chosen == CANDIDATE
    assert note == 'high confidence: Matching concept.'
    assert 'unavailable' not in caplog.text
