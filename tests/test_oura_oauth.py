"""Oura by OAuth: consent, tokens and the API reader (#2900).

Only the HTTP session is faked, so the URLs, form fields and headers asserted
here are the ones the service really sends. No test needs a database.
"""
import base64
import hashlib
import json
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.utils import timezone

from omop_core.services import oura_oauth as oo
from omop_core.services.oura_parser import samples_from_documents

CLIENT_ID = 'oura-client-id'
CLIENT_SECRET = 'oura-client-secret-NOT-REAL'
ACCESS = 'access-token-AAAA'
REFRESH = 'refresh-token-RRRR'
TOKEN_KEY = base64.urlsafe_b64encode(b'k' * 32).decode()
START, END = date(2026, 11, 1), date(2026, 11, 3)


@pytest.fixture(autouse=True)
def oura_settings(settings):
    settings.OURA_CLIENT_ID = CLIENT_ID
    settings.OURA_CLIENT_SECRET = CLIENT_SECRET
    settings.OURA_REDIRECT_URI = ''
    settings.GARMIN_TOKEN_KEY = TOKEN_KEY
    settings.GARMIN_TOKEN_KEY_FALLBACKS = []
    settings.APP_BASE_URL = 'https://promop.example.test/'
    settings.SECRET_KEY = 'unit-test-secret-key'
    settings.SECRET_KEY_FALLBACKS = []
    return settings


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self.text = text if text is not None else ('' if payload is None else json.dumps(payload))

    def json(self):
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as exc:
            raise requests.exceptions.JSONDecodeError(exc.msg, exc.doc, exc.pos) from None


class FakeSession:
    """Answers every request with the next of ``answers`` and records the call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append(SimpleNamespace(method=method, url=url, **kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self):
        self.closed = True


def token_payload(**over):
    payload = {'access_token': ACCESS, 'token_type': 'bearer', 'expires_in': 86400,
               'refresh_token': REFRESH}
    payload.update(over)
    return payload


def page(data=(), next_token=None):
    return FakeResponse(200, {'data': list(data), 'next_token': next_token})


# ── Configuration ───────────────────────────────────────────────────────────

def test_it_is_configured_only_with_an_id_a_secret_and_a_token_key(settings):
    assert oo.configured()
    for name in ('OURA_CLIENT_ID', 'OURA_CLIENT_SECRET', 'GARMIN_TOKEN_KEY'):
        kept = getattr(settings, name)
        setattr(settings, name, '  ')
        assert not oo.configured(), name
        setattr(settings, name, kept)


def test_the_redirect_url_is_built_from_the_base_url_unless_one_is_given(settings):
    assert oo.redirect_uri() == 'https://promop.example.test/api/v1/oura/oauth/callback/'
    settings.OURA_REDIRECT_URI = 'https://other.example.test/cb'
    assert oo.redirect_uri() == 'https://other.example.test/cb'


# ── State and PKCE ──────────────────────────────────────────────────────────

def test_a_state_names_who_started_and_gives_the_same_verifier_each_time():
    state = oo.new_state(41, 7)
    read = oo.read_state(state)
    assert (read.person_id, read.identity_id) == (41, 7)
    assert oo.read_state(state) == read and read.nonce
    assert 43 <= len(read.verifier) <= 128
    other = oo.read_state(oo.new_state(41, 7))
    assert other.verifier != read.verifier and other.nonce != read.nonce and read.verifier not in state


def test_a_state_this_server_did_not_issue_or_issued_too_long_ago_is_nothing(monkeypatch, settings):
    state = oo.new_state(41, 7)
    for junk in (None, '', 5, 'x' * 2000, 'not-a-state'):
        assert oo.read_state(junk) is None
    monkeypatch.setattr(oo, 'STATE_MAX_AGE_SECONDS', -1)
    assert oo.read_state(state) is None
    monkeypatch.undo()
    settings.SECRET_KEY = 'a-different-key'
    assert oo.read_state(state) is None


def test_a_garmin_state_is_not_an_oura_state():
    from omop_core.services import garmin_oauth

    assert oo.read_state(garmin_oauth.new_state(41, 7)) is None


def test_the_consent_url_carries_the_pkce_challenge_the_scopes_and_no_secret():
    state = oo.new_state(41, 7)
    url = urlsplit(oo.authorize_url(state))
    params = {key: values[0] for key, values in parse_qs(url.query).items()}
    assert (url.scheme, url.netloc, url.path) == ('https', 'cloud.ouraring.com', '/oauth/authorize')
    assert params['client_id'] == CLIENT_ID
    assert params['response_type'] == 'code'
    assert params['redirect_uri'] == 'https://promop.example.test/api/v1/oura/oauth/callback/'
    assert params['scope'] == oo.SCOPES and 'personal' not in params['scope'].split()
    assert params['state'] == state and params['code_challenge_method'] == 'S256'
    verifier = oo.read_state(state).verifier
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert params['code_challenge'] == expected
    assert CLIENT_SECRET not in url.geturl() and verifier not in url.geturl()


def test_no_consent_url_without_configuration_or_for_a_bad_state(settings):
    with pytest.raises(oo.OuraOAuthError) as bad:
        oo.authorize_url('junk')
    assert bad.value.status == 400
    settings.OURA_CLIENT_ID = ''
    with pytest.raises(oo.OuraOAuthError) as off:
        oo.authorize_url(oo.new_state(1, 1))
    assert off.value.status == 503


# ── Tokens ──────────────────────────────────────────────────────────────────

def test_the_code_is_exchanged_with_the_secret_and_the_verifier_in_the_body():
    session = FakeSession(FakeResponse(200, token_payload()))
    tokens = oo.exchange_code('the-code', 'the-verifier', session=session)
    (call,) = session.calls
    assert (call.method, call.url) == ('POST', 'https://api.ouraring.com/oauth/token')
    assert call.data == {
        'grant_type': 'authorization_code', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'code': 'the-code', 'code_verifier': 'the-verifier',
        'redirect_uri': 'https://promop.example.test/api/v1/oura/oauth/callback/'}
    assert call.headers['User-Agent'] and call.allow_redirects is False
    assert (tokens.access_token, tokens.refresh_token) == (ACCESS, REFRESH)
    assert tokens.access_expires_at > timezone.now() + timedelta(hours=23)


@pytest.mark.parametrize('code', ['', None, 5, 'has space', 'x' * 3000])
def test_a_code_that_is_not_one_is_not_sent(code):
    session = FakeSession()
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.exchange_code(code, 'v', session=session)
    assert error.value.status == 400 and session.calls == []


@pytest.mark.parametrize('payload', [
    None, [], {}, token_payload(access_token=''), token_payload(refresh_token=None),
    token_payload(expires_in=0), token_payload(expires_in='3600'), token_payload(expires_in=True),
    token_payload(access_token='has space'),
])
def test_a_token_response_that_is_not_one_is_unexpected(payload):
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.exchange_code('c', 'v', session=FakeSession(FakeResponse(200, payload)))
    assert error.value.message == oo.UNEXPECTED


def test_a_refresh_sends_the_refresh_token_and_returns_the_new_pair():
    session = FakeSession(FakeResponse(200, token_payload(access_token='new-a', refresh_token='new-r')))
    tokens = oo.refresh_tokens('old-r', session=session)
    assert session.calls[0].data == {'grant_type': 'refresh_token', 'client_id': CLIENT_ID,
                                     'client_secret': CLIENT_SECRET, 'refresh_token': 'old-r'}
    assert (tokens.access_token, tokens.refresh_token) == ('new-a', 'new-r')


def test_revoking_sends_the_token_as_a_parameter_and_logs_no_query(caplog):
    caplog.set_level('WARNING')
    session = FakeSession(FakeResponse(500))
    with pytest.raises(oo.OuraOAuthError):
        oo.revoke('secret-access-token', session=session)
    call = session.calls[0]
    assert call.url == 'https://api.ouraring.com/oauth/revoke' and call.params == {'access_token': 'secret-access-token'}
    assert 'secret-access-token' not in caplog.text


@pytest.mark.parametrize('status, expected', [(401, 409), (429, 429), (400, 400), (403, 403), (500, 502), (503, 502)])
def test_oura_errors_are_fixed_wording_by_status(status, expected, caplog):
    session = FakeSession(FakeResponse(status, text='secret detail from Oura'))
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.refresh_tokens('r', session=session)
    assert error.value.status == expected and error.value.upstream == status
    assert 'secret detail' not in error.value.message and 'secret detail' not in caplog.text


def test_a_network_failure_or_an_unreadable_answer_is_reported_without_detail():
    with pytest.raises(oo.OuraOAuthError) as down:
        oo.refresh_tokens('r', session=FakeSession(requests.ConnectionError('host oura.example refused')))
    assert down.value.message == oo.UNREACHABLE
    with pytest.raises(oo.OuraOAuthError) as junk:
        oo.refresh_tokens('r', session=FakeSession(FakeResponse(200, text='<html>')))
    assert junk.value.message == oo.UNEXPECTED


def make_connection(**over):
    row = SimpleNamespace(access_token='', refresh_token='', access_expires_at=None,
                          tokens_updated_at=None, saved=[], last_sync_at=None, **over)
    row.save = lambda update_fields=None: row.saved.append(update_fields)
    return row


def test_tokens_are_stored_encrypted_and_read_back():
    from omop_core.services import garmin_oauth

    row = make_connection()
    oo.store_tokens(row, oo.Tokens(ACCESS, REFRESH, timezone.now() + timedelta(hours=1)))
    assert ACCESS not in row.access_token and REFRESH not in row.refresh_token
    assert garmin_oauth.decrypt(row.access_token) == ACCESS
    assert garmin_oauth.decrypt(row.refresh_token) == REFRESH


def test_a_live_access_token_is_used_without_calling_oura():
    row = make_connection()
    oo.store_tokens(row, oo.Tokens(ACCESS, REFRESH, timezone.now() + timedelta(hours=1)))
    session = FakeSession()
    assert oo.usable_access_token(row, session=session) == ACCESS and session.calls == []


def test_an_expired_access_token_is_refreshed_and_both_new_tokens_are_saved_together():
    from omop_core.services import garmin_oauth

    row = make_connection()
    oo.store_tokens(row, oo.Tokens(ACCESS, REFRESH, timezone.now() - timedelta(minutes=1)))
    session = FakeSession(FakeResponse(200, token_payload(access_token='new-a', refresh_token='new-r')))
    assert oo.usable_access_token(row, session=session) == 'new-a'
    assert session.calls[0].data['refresh_token'] == REFRESH
    # Single use: the new refresh token is on the row, saved in the same write.
    assert garmin_oauth.decrypt(row.refresh_token) == 'new-r'
    assert row.saved == [['access_token', 'refresh_token', 'access_expires_at', 'tokens_updated_at']]


@pytest.mark.parametrize('upstream', [400, 401, 403])
def test_a_refresh_oura_refuses_means_connecting_again(upstream):
    row = make_connection()
    oo.store_tokens(row, oo.Tokens(ACCESS, REFRESH, timezone.now() - timedelta(minutes=1)))
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.usable_access_token(row, session=FakeSession(FakeResponse(upstream)))
    assert (error.value.message, error.value.status) == (oo.RECONNECT, 409)
    assert row.saved == []


def test_tokens_that_cannot_be_read_mean_connecting_again(settings):
    row = make_connection()
    oo.store_tokens(row, oo.Tokens(ACCESS, REFRESH, timezone.now() - timedelta(minutes=1)))
    settings.GARMIN_TOKEN_KEY = base64.urlsafe_b64encode(b'z' * 32).decode()
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.usable_access_token(row, session=FakeSession())
    assert error.value.status == 409


# ── Which days to read ──────────────────────────────────────────────────────

TODAY = date(2026, 11, 26)


def test_the_first_sync_reads_ninety_days_and_a_later_one_starts_a_few_days_back():
    assert oo.sync_window(None, TODAY) == (TODAY - timedelta(days=90), TODAY)
    assert oo.sync_window(TODAY - timedelta(days=1), TODAY) == (TODAY - timedelta(days=4), TODAY)
    assert oo.sync_window(TODAY, TODAY) == (TODAY - timedelta(days=3), TODAY)
    # A long gap is cut to the ninety days.
    assert oo.sync_window(TODAY - timedelta(days=400), TODAY)[0] == TODAY - timedelta(days=90)


def test_a_sync_right_after_another_would_only_repeat_it():
    now = timezone.now()
    assert not oo.recently_synced(make_connection())
    fresh = make_connection()
    fresh.last_sync_at = now - timedelta(minutes=1)
    assert oo.recently_synced(fresh, now)
    old = make_connection()
    old.last_sync_at = now - timedelta(minutes=6)
    assert not oo.recently_synced(old, now)


# ── Reading the API ─────────────────────────────────────────────────────────

def test_each_kind_is_read_from_its_endpoint_with_the_bearer_token_and_the_range():
    session = FakeSession(page([{'day': '2026-11-02', 'steps': 5}]), page(), page(), page())
    fetched = oo.fetch_days(ACCESS, START, END, session=session)
    urls = [call.url for call in session.calls]
    assert urls == [f'https://api.ouraring.com/v2/usercollection/{path}'
                    for path in ('daily_activity', 'sleep', 'daily_spo2', 'vO2_max')]
    first = session.calls[0]
    assert first.method == 'GET' and first.headers['Authorization'] == f'Bearer {ACCESS}'
    assert first.headers['User-Agent'] and first.allow_redirects is False
    # The end asked for is a day after the last day wanted.
    assert first.params == {'start_date': '2026-11-01', 'end_date': '2026-11-04'}
    assert fetched.documents['activity'] == [{'day': '2026-11-02', 'steps': 5}]
    assert fetched.unavailable == [] and not fetched.incomplete


def test_pages_are_followed_by_next_token():
    session = FakeSession(page([{'n': 1}], 'tok-2'), page([{'n': 2}], None), page(), page(), page())
    fetched = oo.fetch_days(ACCESS, START, END, session=session)
    assert [d['n'] for d in fetched.documents['activity']] == [1, 2]
    assert 'next_token' not in session.calls[0].params
    assert session.calls[1].params['next_token'] == 'tok-2'
    assert 'next_token' not in session.calls[2].params


def test_a_kind_oura_refuses_is_reported_and_the_rest_are_kept():
    session = FakeSession(page([{'day': '2026-11-02'}]), FakeResponse(403), page(), FakeResponse(403))
    fetched = oo.fetch_days(ACCESS, START, END, session=session)
    assert fetched.unavailable == ['sleep', 'vo2']
    assert fetched.documents['activity'] and fetched.documents['sleep'] == []


def test_refused_for_every_kind_is_no_access():
    session = FakeSession(*[FakeResponse(403)] * 4)
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.fetch_days(ACCESS, START, END, session=session)
    assert (error.value.message, error.value.status) == (oo.NO_ACCESS, 409)


@pytest.mark.parametrize('status, expected', [(401, 409), (429, 429), (500, 502)])
def test_a_rejected_token_a_limit_or_an_outage_fails_the_whole_read(status, expected):
    session = FakeSession(page([{'day': '2026-11-02'}]), FakeResponse(status))
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.fetch_days(ACCESS, START, END, session=session)
    assert error.value.status == expected


@pytest.mark.parametrize('body', [None, [], {'data': 'x'}, {'nodata': []}])
def test_an_answer_that_is_not_a_page_is_unexpected(body):
    with pytest.raises(oo.OuraOAuthError) as error:
        oo.fetch_days(ACCESS, START, END, session=FakeSession(FakeResponse(200, body)))
    assert error.value.message == oo.UNEXPECTED


def test_the_read_stops_starting_calls_when_its_budget_is_spent():
    ticks = iter([0, 0, 100, 100, 100])
    session = FakeSession(page([{'day': '2026-11-02'}]))
    fetched = oo.fetch_days(ACCESS, START, END, session=session, clock=lambda: next(ticks))
    assert len(session.calls) == 1 and fetched.incomplete


def test_pages_that_never_end_are_cut_off_and_said_so():
    session = FakeSession(*[page([{'n': 1}], 'again')] * oo.MAX_PAGES_PER_ENDPOINT,
                          page(), page(), page())
    fetched = oo.fetch_days(ACCESS, START, END, session=session)
    assert fetched.incomplete and len(fetched.documents['activity']) == oo.MAX_PAGES_PER_ENDPOINT


def test_a_session_it_made_is_closed_and_one_it_was_given_is_not(monkeypatch):
    made = FakeSession(page(), page(), page(), page())
    monkeypatch.setattr(oo, '_session', lambda: made)
    oo.fetch_days(ACCESS, START, END)
    assert made.closed
    given = FakeSession(page(), page(), page(), page())
    oo.fetch_days(ACCESS, START, END, session=given)
    assert not given.closed


# ── From documents to days ──────────────────────────────────────────────────

def test_the_apis_documents_become_the_same_days_an_export_gives():
    samples = samples_from_documents({
        'activity': [{'day': '2026-11-02', 'steps': 8200, 'high_activity_time': 600,
                      'medium_activity_time': 1200, 'active_calories': 410}],
        'sleep': [
            {'day': '2026-11-02', 'type': 'long_sleep', 'total_sleep_duration': 25200, 'average_hrv': 52,
             'lowest_heart_rate': 48, 'average_breath': 14.5},
            {'day': '2026-11-02', 'type': 'late_nap', 'total_sleep_duration': 1800, 'average_hrv': 20,
             'lowest_heart_rate': 70, 'average_breath': 16},
            {'day': '2026-11-02', 'type': 'deleted', 'total_sleep_duration': 9999},
        ],
        'spo2': [{'day': '2026-11-02', 'spo2_percentage': {'average': 97.4}}],
        'vo2': [{'day': '2026-11-02', 'vo2_max': 38}],
    })
    by_metric = {s.metric_key: s.value for s in samples}
    assert by_metric == {
        'steps': 8200, 'active_minutes': 30, 'active_energy': 410,
        # Two sessions added; the night's own figures, not the nap's.
        'sleep_duration': 7.5, 'hrv_rmssd': 52, 'resting_hr': 48, 'respiratory_rate': 14.5,
        'spo2': 97.4, 'vo2_max': 38}
    assert {s.date for s in samples} == {date(2026, 11, 2)}


def test_documents_that_are_not_readable_are_skipped_not_fatal():
    samples = samples_from_documents({
        'activity': [{'day': 5, 'steps': 100}, {'day': '2026-11-02', 'steps': 0}, 'junk', None,
                     {'day': '2026-11-03', 'steps': 7000}],
        'sleep': [{'day': '2026-11-03', 'type': None, 'total_sleep_duration': 3600, 'average_hrv': 0}],
        'spo2': [{'day': '2026-11-03', 'spo2_percentage': None}, {'day': '2026-11-03', 'spo2_percentage': True}],
        'vo2': [{'day': '2026-11-03', 'vo2_max': 'high'}],
        'stress': [{'day': '2026-11-03'}],
    })
    assert {(s.metric_key, s.date, s.value) for s in samples} == {
        ('steps', date(2026, 11, 3), 7000), ('sleep_duration', date(2026, 11, 3), 1.0)}


def test_a_day_repeated_in_the_documents_is_the_later_one():
    samples = samples_from_documents({'activity': [{'day': '2026-11-02', 'steps': 100},
                                                   {'day': '2026-11-02', 'steps': 300}]})
    assert [(s.metric_key, s.value) for s in samples] == [('steps', 300)]
