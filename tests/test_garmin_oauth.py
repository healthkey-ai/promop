"""Garmin Connect by OAuth: consent, tokens and the push reader (#2802).

Only the HTTP session is faked, so the URLs, form fields and headers asserted
here are the ones the service really sends. No test needs a database.
"""
import base64
import hashlib
import json
import logging
from datetime import date, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.utils import timezone

from omop_core.services import garmin_oauth as go
from omop_core.services.wearable_parsers import WearableSample

CLIENT_ID = '600a2456-ab5f-4b1f-8eb3-8cc8dc86264d'
CLIENT_SECRET = 'test-client-secret-NOT-REAL'
ACCESS = 'access-token-AAAA'
REFRESH = 'refresh-token-RRRR=='
USER = 'd3315b1072421d0dd7c8f6b8e1de4df8'


@pytest.fixture(autouse=True)
def garmin_settings(settings):
    settings.GARMIN_CLIENT_ID = CLIENT_ID
    settings.GARMIN_CLIENT_SECRET = CLIENT_SECRET
    settings.GARMIN_WEBHOOK_SECRET = 'hook-secret'
    settings.GARMIN_REDIRECT_URI = ''
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
    payload = {'access_token': ACCESS, 'expires_in': 86400, 'token_type': 'bearer',
               'refresh_token': REFRESH, 'scope': 'PARTNER_READ', 'jti': 'x',
               'refresh_token_expires_in': 7775998}
    payload.update(over)
    return payload


# ── Configuration ───────────────────────────────────────────────────────────

def test_it_is_configured_only_with_the_key_the_secret_and_the_webhook_secret(settings):
    assert go.configured()
    for name in ('GARMIN_CLIENT_ID', 'GARMIN_CLIENT_SECRET', 'GARMIN_WEBHOOK_SECRET'):
        kept = getattr(settings, name)
        setattr(settings, name, '  ')
        assert not go.configured(), name
        setattr(settings, name, kept)


def test_the_redirect_url_is_built_from_the_base_url_unless_one_is_given(settings):
    assert go.redirect_uri() == 'https://promop.example.test/api/v1/garmin/oauth/callback/'
    settings.GARMIN_REDIRECT_URI = 'https://other.example.test/cb'
    assert go.redirect_uri() == 'https://other.example.test/cb'


# ── State and PKCE ──────────────────────────────────────────────────────────

def test_a_state_names_who_started_and_gives_the_same_verifier_each_time():
    state = go.new_state(41, 7)
    person_id, identity_id, verifier = go.read_state(state)
    assert (person_id, identity_id) == (41, 7)
    assert go.read_state(state)[2] == verifier
    # 43 to 128 characters of the unreserved set, as the specification asks.
    assert 43 <= len(verifier) <= 128 and set(verifier) <= set(
        'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~')
    # Two starts never share a verifier, and the state does not contain it.
    other = go.read_state(go.new_state(41, 7))[2]
    assert other != verifier and verifier not in state


def test_a_state_this_server_did_not_issue_or_issued_too_long_ago_is_nothing(monkeypatch, settings):
    state = go.new_state(41, 7)
    assert go.read_state(state[:-2] + ('aa' if not state.endswith('aa') else 'bb')) is None
    for junk in (None, '', 5, 'x' * 2000, 'not-a-state'):
        assert go.read_state(junk) is None
    monkeypatch.setattr(go, 'STATE_MAX_AGE_SECONDS', -1)
    assert go.read_state(state) is None
    monkeypatch.undo()
    # Another deployment's key signs nothing this one accepts.
    settings.SECRET_KEY = 'a-different-key'
    assert go.read_state(state) is None


def test_the_verifier_cannot_be_derived_without_this_deployments_key(settings):
    state = go.new_state(41, 7)
    here = go.read_state(state)[2]
    settings.SECRET_KEY_FALLBACKS = ['unit-test-secret-key']
    settings.SECRET_KEY = 'rotated-key'
    # The old state still verifies through the fallback, but the verifier is
    # the new key's: a connection begun before a rotation has to be restarted.
    read = go.read_state(state)
    assert read is not None and read[2] != here


def test_the_consent_url_carries_the_challenge_of_the_states_verifier():
    state = go.new_state(41, 7)
    url = urlsplit(go.authorize_url(state))
    assert f'{url.scheme}://{url.netloc}{url.path}' == 'https://connect.garmin.com/oauth2Confirm'
    params = {key: values[0] for key, values in parse_qs(url.query).items()}
    verifier = go.read_state(state)[2]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert params == {
        'response_type': 'code', 'client_id': CLIENT_ID, 'code_challenge': challenge,
        'code_challenge_method': 'S256', 'state': state,
        'redirect_uri': 'https://promop.example.test/api/v1/garmin/oauth/callback/',
    }
    assert CLIENT_SECRET not in url.query and verifier not in url.query


def test_no_consent_url_without_configuration_or_for_a_bad_state(settings):
    with pytest.raises(go.GarminOAuthError) as bad:
        go.authorize_url('junk')
    assert bad.value.status == 400
    settings.GARMIN_CLIENT_SECRET = ''
    with pytest.raises(go.GarminOAuthError) as unset:
        go.authorize_url(go.new_state(41, 7))
    assert unset.value.status == 503 and unset.value.message == go.NOT_CONFIGURED


# ── Token endpoint ──────────────────────────────────────────────────────────

def test_the_code_is_exchanged_as_the_specification_says():
    session = FakeSession(FakeResponse(payload=token_payload()))
    before = timezone.now()
    tokens = go.exchange_code('the-code', 'the-verifier', session=session)
    call = session.calls[0]
    assert (call.method, call.url) == ('POST', 'https://diauth.garmin.com/di-oauth2-service/oauth/token')
    assert call.data == {
        'grant_type': 'authorization_code', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'code': 'the-code', 'code_verifier': 'the-verifier',
        'redirect_uri': 'https://promop.example.test/api/v1/garmin/oauth/callback/',
    }
    # The secret goes to Garmin's token host and nowhere a redirect points.
    assert call.allow_redirects is False and call.timeout == (go.CONNECT_TIMEOUT_SECONDS, go.REQUEST_TIMEOUT_SECONDS)
    assert (tokens.access_token, tokens.refresh_token) == (ACCESS, REFRESH)
    assert timedelta(seconds=86390) < tokens.access_expires_at - before < timedelta(seconds=86410)
    assert timedelta(days=89) < tokens.refresh_expires_at - before < timedelta(days=91)
    # A session the caller passed is the caller's to close.
    assert session.closed is False


def test_a_refresh_sends_the_refresh_token_and_returns_the_pair_that_replaces_it():
    session = FakeSession(FakeResponse(payload=token_payload(access_token='new-access', refresh_token='new-refresh')))
    tokens = go.refresh_tokens(REFRESH, session=session)
    assert session.calls[0].data == {
        'grant_type': 'refresh_token', 'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET,
        'refresh_token': REFRESH}
    assert (tokens.access_token, tokens.refresh_token) == ('new-access', 'new-refresh')


@pytest.mark.parametrize('payload', [
    None, [], 'text', {}, token_payload(access_token=''), token_payload(access_token=5),
    token_payload(refresh_token=None), token_payload(expires_in='86400'), token_payload(expires_in=True),
    token_payload(expires_in=-5), token_payload(expires_in=10 ** 12), token_payload(access_token='has space'),
])
def test_a_token_answer_that_is_not_one_is_refused(payload):
    session = FakeSession(FakeResponse(payload=payload) if payload is not None else FakeResponse(text='not json'))
    with pytest.raises(go.GarminOAuthError) as error:
        go.exchange_code('the-code', 'the-verifier', session=session)
    assert error.value.message == go.UNEXPECTED and error.value.status == 502


def test_a_missing_refresh_lifetime_is_left_unknown():
    payload = token_payload()
    del payload['refresh_token_expires_in']
    tokens = go.exchange_code('c', 'v', session=FakeSession(FakeResponse(payload=payload)))
    assert tokens.refresh_expires_at is None


@pytest.mark.parametrize('status, answer', [(400, 400), (401, 400), (403, 400), (404, 502), (429, 502), (500, 502), (302, 502)])
def test_garmin_refusing_is_reported_by_status_alone(status, answer, caplog):
    body = f'{{"error":"invalid_grant","detail":"secret {CLIENT_SECRET} code the-code"}}'
    session = FakeSession(FakeResponse(status=status, text=body))
    with caplog.at_level(logging.DEBUG), pytest.raises(go.GarminOAuthError) as error:
        go.exchange_code('the-code', 'the-verifier', session=session)
    assert error.value.status == answer
    # Fixed wording: nothing Garmin said, and none of what was sent.
    assert error.value.message in (go.DENIED, go.UNREACHABLE)
    assert 'invalid_grant' not in error.value.message
    for secret in (CLIENT_SECRET, 'the-code', 'the-verifier'):
        assert secret not in caplog.text


@pytest.mark.parametrize('failure', [requests.ConnectTimeout('t'), requests.ConnectionError('c'), requests.ReadTimeout('r')])
def test_a_network_failure_is_unreachable_and_logs_no_credential(failure, caplog):
    with caplog.at_level(logging.DEBUG), pytest.raises(go.GarminOAuthError) as error:
        go.refresh_tokens(REFRESH, session=FakeSession(failure))
    assert (error.value.message, error.value.status) == (go.UNREACHABLE, 502)
    assert REFRESH not in caplog.text and CLIENT_SECRET not in caplog.text


def test_a_code_that_could_not_be_one_is_refused_before_any_request():
    session = FakeSession()
    for code in ('', None, 'two words', 'x' * 5000, 'line\nbreak'):
        with pytest.raises(go.GarminOAuthError) as error:
            go.exchange_code(code, 'v', session=session)
        assert error.value.status == 400
    assert session.calls == []


# ── The wellness API ────────────────────────────────────────────────────────

def test_the_user_id_and_permissions_are_read_with_the_bearer_token():
    session = FakeSession(FakeResponse(payload={'userId': USER}),
                          FakeResponse(payload=['HEALTH_EXPORT', 'ACTIVITY_EXPORT']))
    assert go.fetch_user_id(ACCESS, session=session) == USER
    assert go.fetch_permissions(ACCESS, session=session) == ['ACTIVITY_EXPORT', 'HEALTH_EXPORT']
    assert [(c.method, c.url) for c in session.calls] == [
        ('GET', 'https://apis.garmin.com/wellness-api/rest/user/id'),
        ('GET', 'https://apis.garmin.com/wellness-api/rest/user/permissions')]
    assert all(c.headers['Authorization'] == f'Bearer {ACCESS}' for c in session.calls)


@pytest.mark.parametrize('payload', [{}, {'userId': ''}, {'userId': 5}, {'userId': 'a b'}, {'userId': 'x' * 80}, []])
def test_a_user_id_that_is_not_one_is_refused(payload):
    with pytest.raises(go.GarminOAuthError) as error:
        go.fetch_user_id(ACCESS, session=FakeSession(FakeResponse(payload=payload)))
    assert error.value.message == go.UNEXPECTED


@pytest.mark.parametrize('value, cleaned', [
    (['HEALTH_EXPORT', 'HEALTH_EXPORT', 5, None, 'bad name', '<script>'], ['HEALTH_EXPORT']),
    ({'permissions': ['MCT_EXPORT', 'HEALTH_EXPORT']}, ['HEALTH_EXPORT', 'MCT_EXPORT']),
    ({'healthExport': True, 'activityExport': False}, ['healthExport']),
    ('HEALTH_EXPORT', []), (None, []), (7, []),
])
def test_permissions_are_names_only_whatever_shape_they_arrive_in(value, cleaned):
    assert go.clean_permissions(value) == cleaned


def test_deregistering_deletes_the_registration_and_needs_no_body():
    session = FakeSession(FakeResponse(status=204, text=''))
    go.deregister(ACCESS, session=session)
    call = session.calls[0]
    assert (call.method, call.url) == ('DELETE', 'https://apis.garmin.com/wellness-api/rest/user/registration')
    assert call.headers['Authorization'] == f'Bearer {ACCESS}'


# ── Tokens at rest ──────────────────────────────────────────────────────────

def test_a_stored_token_is_not_the_token_and_reads_back(settings):
    stored = go.encrypt(REFRESH)
    assert REFRESH not in stored and go.decrypt(stored) == REFRESH
    assert go.encrypt(REFRESH) != stored          # a fresh nonce each time
    for unreadable in ('', None, 'plain-text', stored[:-4] + 'AAAA'):
        assert go.decrypt(unreadable) is None
    # Restored on a deployment with another key, it is nothing.
    settings.SECRET_KEY = 'another-deployment'
    assert go.decrypt(stored) is None
    # A rotated key still reads what the old one wrote, and writes with the new.
    settings.SECRET_KEY_FALLBACKS = ['unit-test-secret-key']
    assert go.decrypt(stored) == REFRESH
    rewritten = go.encrypt(REFRESH)
    settings.SECRET_KEY_FALLBACKS = []
    assert go.decrypt(rewritten) == REFRESH and go.decrypt(stored) is None


class Row(SimpleNamespace):
    """Stands in for a GarminConnection: the fields and a save that is counted."""

    def save(self, update_fields=None):
        self.saved = list(update_fields or [])


def row(expires_in, **over):
    fields = dict(access_token=go.encrypt(ACCESS), refresh_token=go.encrypt(REFRESH),
                  access_expires_at=timezone.now() + timedelta(seconds=expires_in),
                  refresh_expires_at=None, tokens_updated_at=None, saved=None)
    fields.update(over)
    return Row(**fields)


def test_an_access_token_with_time_left_is_used_as_it_is():
    connection = row(3600)
    session = FakeSession()
    assert go.usable_access_token(connection, session=session) == ACCESS
    assert session.calls == [] and connection.saved is None


def test_an_access_token_within_ten_minutes_of_expiry_is_refreshed_and_both_tokens_saved():
    connection = row(go.EXPIRY_MARGIN_SECONDS - 5)
    session = FakeSession(FakeResponse(payload=token_payload(access_token='new-access', refresh_token='new-refresh')))
    assert go.usable_access_token(connection, session=session) == 'new-access'
    assert session.calls[0].data['refresh_token'] == REFRESH
    assert go.decrypt(connection.access_token) == 'new-access'
    assert go.decrypt(connection.refresh_token) == 'new-refresh'
    assert set(connection.saved) == {'access_token', 'refresh_token', 'access_expires_at',
                                     'refresh_expires_at', 'tokens_updated_at'}
    assert connection.tokens_updated_at is not None


def test_tokens_that_cannot_be_read_ask_for_a_new_connection_without_calling_garmin():
    session = FakeSession()
    for connection in (row(-5, refresh_token=''), row(-5, refresh_token='not-encrypted'),
                       row(3600, access_token='garbage', refresh_token='')):
        with pytest.raises(go.GarminOAuthError) as error:
            go.usable_access_token(connection, session=session)
        assert error.value.status == 400
    assert session.calls == []


# ── Push ────────────────────────────────────────────────────────────────────

DAY = date(2026, 11, 20)


def values(push, user=USER):
    return {(s.metric_key, s.date): s.value for s in push.samples.get(user, [])}


def test_a_daily_summary_becomes_the_days_six_metrics():
    push = go.read_push({'dailies': [{
        'userId': USER, 'userAccessToken': 'ignored', 'summaryId': 'x', 'calendarDate': '2026-11-20',
        'steps': 8123, 'distanceInMeters': 6400.4, 'activeKilocalories': 450, 'bmrKilocalories': 1700,
        'restingHeartRateInBeatsPerMinute': 52,
        'moderateIntensityDurationInSeconds': 1200, 'vigorousIntensityDurationInSeconds': 630,
        'averageStressLevel': 30, 'floorsClimbed': 4,
    }]})
    assert values(push) == {
        ('steps', DAY): 8123.0, ('distance', DAY): 6.4, ('active_energy', DAY): 450.0,
        ('basal_energy', DAY): 1700.0, ('resting_hr', DAY): 52.0, ('active_minutes', DAY): 30.5,
    }
    assert all(isinstance(sample, WearableSample) for sample in push.samples[USER])
    assert push.deregistered == set() and push.permissions == {}


def test_sleep_is_the_sleep_stages_and_falls_back_to_the_duration():
    push = go.read_push({'sleeps': [
        {'userId': USER, 'calendarDate': '2026-11-20', 'durationInSeconds': 30000,
         'deepSleepDurationInSeconds': 5400, 'lightSleepDurationInSeconds': 14400,
         'remSleepInSeconds': 5400, 'awakeDurationInSeconds': 1800},
        {'userId': 'other-user', 'calendarDate': '2026-11-20', 'durationInSeconds': 27000},
        {'userId': 'no-sleep', 'calendarDate': '2026-11-20', 'durationInSeconds': 0},
    ]})
    assert values(push) == {('sleep_duration', DAY): 7.0}
    assert values(push, 'other-user') == {('sleep_duration', DAY): 7.5}
    assert 'no-sleep' not in push.samples


def test_hrv_and_vo2_max_are_read_from_their_own_types():
    push = go.read_push({
        'hrv': [{'userId': USER, 'calendarDate': '2026-11-20', 'lastNightAvg': 44, 'lastNight5MinHigh': 72}],
        'userMetrics': [{'userId': USER, 'calendarDate': '2026-11-20', 'vo2Max': 48.0, 'fitnessAge': 32}],
    })
    assert values(push) == {('hrv_rmssd', DAY): 44.0, ('vo2_max', DAY): 48.0}


def test_pulse_ox_and_respiration_are_averaged_over_the_days_readings():
    # 2026-11-20 23:00 UTC, five hours west of it: still the 20th for the wearer.
    late = 1795215600
    push = go.read_push({
        'pulseox': [{'userId': USER, 'calendarDate': '2026-11-20',
                     'timeOffsetSpo2Values': {'0': 94, '60': 96, '120': 0, '180': 'x'}}],
        'allDayRespiration': [
            {'userId': USER, 'startTimeInSeconds': late, 'startTimeOffsetInSeconds': -18000,
             'timeOffsetEpochToBreaths': {'0': 14.0, '60': 16.0}},
            {'userId': USER, 'startTimeInSeconds': late - 3600, 'startTimeOffsetInSeconds': -18000,
             'timeOffsetEpochToBreaths': {'0': 12.0}},
        ],
    })
    assert values(push) == {('spo2', DAY): 95.0, ('respiratory_rate', DAY): 13.5}


def test_an_entry_with_no_calendar_date_takes_the_local_day_it_started_on():
    # 2026-11-21 02:00 UTC is the evening of the 20th five hours west.
    start = 1795226400
    push = go.read_push({'dailies': [
        {'userId': USER, 'startTimeInSeconds': start, 'startTimeOffsetInSeconds': -18000, 'steps': 10},
        {'userId': 'utc', 'startTimeInSeconds': start, 'steps': 11},
        {'userId': 'nowhen', 'steps': 12},
        {'userId': 'badday', 'calendarDate': 'tomorrow', 'steps': 13},
    ]})
    assert values(push) == {('steps', DAY): 10.0}
    assert values(push, 'utc') == {('steps', date(2026, 11, 21)): 11.0}
    assert 'nowhen' not in push.samples and 'badday' not in push.samples


def test_a_later_entry_for_the_same_day_wins_and_every_user_has_their_own():
    push = go.read_push({'dailies': [
        {'userId': USER, 'calendarDate': '2026-11-20', 'steps': 4000},
        {'userId': 'someone-else', 'calendarDate': '2026-11-20', 'steps': 99},
        {'userId': USER, 'calendarDate': '2026-11-20', 'steps': 9000},
    ]})
    assert values(push) == {('steps', DAY): 9000.0}
    assert values(push, 'someone-else') == {('steps', DAY): 99.0}


def test_deregistrations_and_permission_changes_are_reported_by_user():
    push = go.read_push({
        'deregistrations': [{'userId': USER, 'userAccessToken': 't'}, {'userId': 5}, 'junk'],
        'userPermissionsChange': [
            {'userId': 'changed', 'permissions': ['HEALTH_EXPORT', 'ACTIVITY_EXPORT'], 'changeTimeInSeconds': 1},
            {'userId': 'revoked', 'permissions': []}],
    })
    assert push.deregistered == {USER}
    assert push.permissions == {'changed': ['ACTIVITY_EXPORT', 'HEALTH_EXPORT'], 'revoked': []}
    assert push.samples == {}


@pytest.mark.parametrize('payload', [
    None, [], 'text', 5, {}, {'dailies': None}, {'dailies': 'x'}, {'dailies': [None, 5, 'x', []]},
    {'dailies': [{'calendarDate': '2026-11-20', 'steps': 5}]},                       # no user
    {'dailies': [{'userId': 'a b', 'calendarDate': '2026-11-20', 'steps': 5}]},      # not an id
    {'dailies': [{'userId': USER, 'calendarDate': '2026-11-20', 'steps': True}]},    # a flag, not a count
    {'dailies': [{'userId': USER, 'calendarDate': '2026-11-20', 'steps': 'many'}]},
    {'dailies': [{'userId': USER, 'calendarDate': '2026-11-20', 'steps': float('nan')}]},
    {'stressDetails': [{'userId': USER, 'calendarDate': '2026-11-20', 'averageStressLevel': 30}]},
    {'activities': [{'userId': USER, 'steps': 500}]},
])
def test_a_body_that_holds_nothing_storable_is_read_as_nothing(payload):
    push = go.read_push(payload)
    assert push.samples == {} and push.deregistered == set() and push.permissions == {}
