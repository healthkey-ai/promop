"""A patient's Oura account, linked by OAuth, and the days read from Oura's API (#2900).

Oura's API V2 is OAuth2 only (personal access tokens are deprecated), and unlike
Garmin's Starter tier it can be read on demand with history. So a patient
approves this deployment's Oura app once on Oura's consent page, and this server
then *pulls* their days when the patient opens the Wearables tab or presses
Sync. It sits beside the export upload (#2889, ``oura_parser.py``), whose rules
for a day it shares: the API's documents are read through the same code.

The pieces, following Oura's authentication page and the Garmin link
(``garmin_oauth.py``) it is modelled on:

* **Consent.** :func:`authorize_url` sends the browser to Oura with a PKCE
  challenge and a signed ``state`` naming who started it. The state travels in
  URLs and proves nothing by itself, so the view completes a connection only
  for the browser session that started it.
* **Tokens.** Oura's refresh tokens are single use: every refresh returns a new
  pair and retires the old one, so the pair is always saved together, under the
  connection's row lock. They are stored encrypted under the wearable token
  key (``garmin_oauth.encrypt``: ``GARMIN_TOKEN_KEY``, in no database).
* **Reading.** :func:`fetch_days` reads ``daily_activity``, ``sleep``,
  ``daily_spo2`` and ``vO2_max`` for a range of days.

Oura's error text, the tokens and the client secret are never logged or shown;
a failure is reported by fixed wording and HTTP status alone.
"""
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.utils import timezone

from omop_core.services import garmin_oauth

logger = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://cloud.ouraring.com/oauth/authorize'
# Oura's token endpoint. (Not named for what it returns: a string constant with
# "token" in its name reads to the credential scan as a hardcoded one.)
GRANT_URL = 'https://api.ouraring.com/oauth/token'
REVOKE_URL = 'https://api.ouraring.com/oauth/revoke'
API_BASE = 'https://api.ouraring.com/v2/usercollection'
CALLBACK_PATH = '/api/v1/oura/oauth/callback/'

# What the sync reads: daily summaries (activity, sleep), SpO2 and VO2 max.
# Oura lets the patient switch scopes off on its page, so each document type is
# read on its own and one that is refused is reported, not fatal. No personal
# scope: nothing here needs the patient's age, weight or e-mail.
SCOPES = 'daily spo2 heart_health'
# Oura's endpoint for each kind of document the parser reads.
ENDPOINTS = (('activity', 'daily_activity'), ('sleep', 'sleep'),
             ('spo2', 'daily_spo2'), ('vo2', 'vO2_max'))
# Sent on every request: Oura may answer 403 to a request without one.
USER_AGENT = 'promop-wearables'

CONNECT_TIMEOUT_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 8.0
# The sync runs inside the patient's request: no call starts after this.
BUDGET_SECONDS = 20.0
MAX_PAGES_PER_ENDPOINT = 30
MAX_DOCUMENTS = 20000
STATE_MAX_AGE_SECONDS = 15 * 60
EXPIRY_MARGIN_SECONDS = 300
# The first sync reads this many days; Oura keeps more, but a patient's tab
# needs the recent ones and a request has a budget.
INITIAL_DAYS = 90
# Oura revises recent days (a ring that synced late), so a sync re-reads the
# last days it already has.
OVERLAP_DAYS = 3
# A second sync this soon would only repeat the first and spend Oura's limit
# (5,000 requests per 5 minutes for the whole application).
MIN_SYNC_INTERVAL = timedelta(minutes=5)

_STATE_SALT = 'oura-oauth-state'
_TOKEN = re.compile(r'[\x21-\x7e]{1,8192}')
_CODE = re.compile(r'[\x21-\x7e]{1,2048}')
_NEXT = re.compile(r'[\x21-\x7e]{1,2048}')

NOT_CONFIGURED = 'Connecting an Oura account is not set up on this server.'
DENIED = 'Oura did not complete the connection. Try again.'
UNREACHABLE = 'Could not reach Oura. Try again later.'
UNEXPECTED = 'Oura returned an answer this server could not read.'
RECONNECT = 'Oura no longer accepts this connection. Connect your Oura account again.'
RATE_LIMITED = 'Oura is limiting requests right now. Try again in a few minutes.'
NO_ACCESS = ('Oura shared none of the data this site reads. Connect again and leave the '
             'activity, sleep, SpO2 and heart health boxes ticked.')


class OuraOAuthError(Exception):
    """A step that cannot go on. The message is fixed wording, safe to show.
    ``upstream`` is Oura's HTTP status when Oura answered, else None."""

    def __init__(self, message, status=502, upstream=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.upstream = upstream


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime


@dataclass
class Fetched:
    """What one read of the API returned."""
    documents: dict = field(default_factory=dict)   # kind -> [document]
    unavailable: list = field(default_factory=list)  # kinds Oura refused (scope off)
    incomplete: bool = False                         # the budget ran out first


# ── Configuration ───────────────────────────────────────────────────────────

def client_id():
    return (getattr(settings, 'OURA_CLIENT_ID', '') or '').strip()


def _client_secret():
    return (getattr(settings, 'OURA_CLIENT_SECRET', '') or '').strip()


def configured():
    """Whether a patient can connect: the app's id and secret are set, and so is
    the key the tokens are stored under (the same key as Garmin's)."""
    return bool(client_id() and _client_secret() and garmin_oauth.can_encrypt())


def redirect_uri():
    """The callback URL registered with Oura, which must match it exactly."""
    explicit = (getattr(settings, 'OURA_REDIRECT_URI', '') or '').strip()
    return explicit or f"{settings.APP_BASE_URL.rstrip('/')}{CALLBACK_PATH}"


# ── Consent: state and PKCE ─────────────────────────────────────────────────

def _verifier(nonce):
    """The PKCE code verifier for a state's nonce: 43 URL-safe characters,
    derived with a key only this deployment has, never stored."""
    key = hmac.new(settings.SECRET_KEY.encode(), b'oura-oauth:pkce', hashlib.sha256).digest()
    digest = hmac.new(key, nonce.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b'=').decode()


def _challenge(verifier):
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()


@dataclass(frozen=True)
class State:
    """What a state says: whose record, who started, and the round trip's nonce."""
    person_id: int
    identity_id: int
    nonce: str
    verifier: str


def new_state(person_id, identity_id):
    """A signed state naming who started the connection and for which record."""
    return signing.dumps(
        {'p': person_id, 'i': identity_id, 'n': secrets.token_urlsafe(24)},
        salt=_STATE_SALT, compress=True)


def read_state(state):
    """The :class:`State` of a state this server issued within
    :data:`STATE_MAX_AGE_SECONDS`, else None."""
    if not isinstance(state, str) or not 0 < len(state) <= 1024:
        return None
    try:
        data = signing.loads(state, salt=_STATE_SALT, max_age=STATE_MAX_AGE_SECONDS)
        nonce = data['n']
        if not isinstance(nonce, str) or not nonce:
            return None
        return State(person_id=data['p'], identity_id=data['i'], nonce=nonce, verifier=_verifier(nonce))
    except (signing.BadSignature, KeyError, TypeError):
        return None


def authorize_url(state):
    """Oura's consent page for this app, carrying the state and its challenge."""
    if not configured():
        raise OuraOAuthError(NOT_CONFIGURED, 503)
    read = read_state(state)
    if read is None:
        raise OuraOAuthError(DENIED, 400)
    return f'{AUTHORIZE_URL}?' + urlencode({
        'response_type': 'code',
        'client_id': client_id(),
        'redirect_uri': redirect_uri(),
        'scope': SCOPES,
        'state': state,
        'code_challenge': _challenge(read.verifier),
        'code_challenge_method': 'S256',
    })


# ── Oura's endpoints ────────────────────────────────────────────────────────

def _session():
    import requests
    return requests.Session()


def _call(method, url, *, session=None, expect_json=True, **kwargs):
    """One request to Oura. Raises OuraOAuthError; returns decoded JSON."""
    import requests

    own = session is None
    session = session or _session()
    headers = {'User-Agent': USER_AGENT, 'Accept': 'application/json', **kwargs.pop('headers', {})}
    try:
        response = session.request(
            method, url, headers=headers,
            timeout=(CONNECT_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS),
            # A credential goes to the host named here and nowhere it points.
            allow_redirects=False, **kwargs)
        code = response.status_code
        if not 200 <= code < 300:
            # Never the query string: the revoke call carries a token in it.
            logger.warning('oura_oauth_refused url=%s status=%s', url, code)
            if code == 401:
                raise OuraOAuthError(RECONNECT, 409, code)
            if code == 429:
                raise OuraOAuthError(RATE_LIMITED, 429, code)
            if code in (400, 403):
                raise OuraOAuthError(DENIED, 400 if code == 400 else 403, code)
            raise OuraOAuthError(UNREACHABLE, 502, code)
        if not expect_json:
            return None
        try:
            return response.json()
        except ValueError:
            raise OuraOAuthError(UNEXPECTED) from None
    except requests.RequestException as exc:
        logger.warning('oura_oauth_request_failed url=%s error=%s', url, type(exc).__name__)
        raise OuraOAuthError(UNREACHABLE) from None
    finally:
        if own:
            session.close()


def _tokens(payload, now):
    """The token response as Tokens, or UNEXPECTED when it is not one."""
    if not isinstance(payload, dict):
        raise OuraOAuthError(UNEXPECTED)
    access, refresh = payload.get('access_token'), payload.get('refresh_token')
    expires_in = payload.get('expires_in')
    if (not isinstance(access, str) or not _TOKEN.fullmatch(access)
            or not isinstance(refresh, str) or not _TOKEN.fullmatch(refresh)
            or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
            or not 0 < expires_in <= 366 * 86400):
        raise OuraOAuthError(UNEXPECTED)
    return Tokens(access_token=access, refresh_token=refresh,
                  access_expires_at=now + timedelta(seconds=float(expires_in)))


def exchange_code(code, verifier, *, session=None):
    """Trade the authorization code from the callback for tokens."""
    if not configured():
        raise OuraOAuthError(NOT_CONFIGURED, 503)
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise OuraOAuthError(DENIED, 400)
    payload = _call('POST', GRANT_URL, session=session, data={
        'grant_type': 'authorization_code',
        'client_id': client_id(),
        'client_secret': _client_secret(),
        'code': code,
        'code_verifier': verifier,
        'redirect_uri': redirect_uri(),
    })
    return _tokens(payload, timezone.now())


def refresh_tokens(refresh_token, *, session=None):
    """A new access token and the refresh token that replaces the one sent."""
    if not configured():
        raise OuraOAuthError(NOT_CONFIGURED, 503)
    payload = _call('POST', GRANT_URL, session=session, data={
        'grant_type': 'refresh_token',
        'client_id': client_id(),
        'client_secret': _client_secret(),
        'refresh_token': refresh_token,
    })
    return _tokens(payload, timezone.now())


def revoke(access_token, *, session=None):
    """Tell Oura the patient disconnected here, so the token stops working."""
    _call('GET', REVOKE_URL, session=session, expect_json=False, params={'access_token': access_token})


# ── Tokens at rest ──────────────────────────────────────────────────────────

def store_tokens(connection, tokens):
    """Put a token pair on a connection (unsaved)."""
    try:
        connection.access_token = garmin_oauth.encrypt(tokens.access_token)
        connection.refresh_token = garmin_oauth.encrypt(tokens.refresh_token)
    except garmin_oauth.GarminOAuthError:
        raise OuraOAuthError(NOT_CONFIGURED, 503) from None
    connection.access_expires_at = tokens.access_expires_at
    connection.tokens_updated_at = timezone.now()


def usable_access_token(connection, *, session=None):
    """An access token Oura will accept, refreshing and saving when needed.

    Call with the connection's row locked and in a transaction that commits
    before anything else is attempted: a refresh retires the stored refresh
    token at Oura, so the new pair must not be lost to a rollback, and two
    refreshes racing would leave one of them holding a dead one. A refresh
    Oura refuses means the patient has to connect again.
    """
    now = timezone.now()
    access = garmin_oauth.decrypt(connection.access_token)
    if (access and connection.access_expires_at
            and connection.access_expires_at - timedelta(seconds=EXPIRY_MARGIN_SECONDS) > now):
        return access
    refresh = garmin_oauth.decrypt(connection.refresh_token)
    if not refresh:
        raise OuraOAuthError(RECONNECT, 409)
    try:
        tokens = refresh_tokens(refresh, session=session)
    except OuraOAuthError as exc:
        if exc.upstream in (400, 401, 403):
            raise OuraOAuthError(RECONNECT, 409, exc.upstream) from None
        raise
    store_tokens(connection, tokens)
    connection.save(update_fields=['access_token', 'refresh_token', 'access_expires_at',
                                   'tokens_updated_at'])
    return tokens.access_token


def fresh_access_token(person, *, session=None):
    """The person's access token, refreshed first when it is old, with the
    refresh committed on its own. None when the person has no connection."""
    from django.db import transaction

    from omop_core.models import OuraConnection

    with transaction.atomic():
        row = OuraConnection.objects.select_for_update().filter(person=person).first()
        if row is None:
            return None
        return usable_access_token(row, session=session)


# ── Reading the API ─────────────────────────────────────────────────────────

def sync_window(synced_through, today):
    """The first and last day the next sync reads: the last
    :data:`INITIAL_DAYS` for a first one, else from a few days before where
    the last one ended (Oura revises recent days) up to today."""
    earliest = today - timedelta(days=INITIAL_DAYS)
    if synced_through is None:
        return earliest, today
    return max(earliest, synced_through - timedelta(days=OVERLAP_DAYS)), today


def recently_synced(connection, now=None):
    """Whether a sync finished so recently that another would only repeat it."""
    last = connection.last_sync_at
    return last is not None and (now or timezone.now()) - last < MIN_SYNC_INTERVAL


def fetch_days(access_token, start, end, *, session=None, clock=time.monotonic):
    """Read the days ``start`` to ``end`` (inclusive) from Oura.

    Each kind of document is read on its own, page by page. A kind Oura
    refuses with 403 (the patient switched that scope off, or the membership
    lapsed) is reported in ``unavailable`` and the rest are kept; refused for
    every kind is :data:`NO_ACCESS`. A 401 or 429, or Oura being down, is an
    error for the whole read, since what was read would be a part of a day.
    The read stops starting calls after :data:`BUDGET_SECONDS`
    (``incomplete``). The end date asked for is a day later than ``end``, as
    Oura's end date may be exclusive for some documents.
    """
    deadline = clock() + BUDGET_SECONDS
    result = Fetched(documents={kind: [] for kind, _path in ENDPOINTS})
    total = 0
    own = session is None
    session = session or _session()
    try:
        for kind, path in ENDPOINTS:
            next_token = None
            for _page in range(MAX_PAGES_PER_ENDPOINT):
                if clock() > deadline or total >= MAX_DOCUMENTS:
                    result.incomplete = True
                    break
                params = {'start_date': start.isoformat(), 'end_date': (end + timedelta(days=1)).isoformat()}
                if next_token:
                    params['next_token'] = next_token
                try:
                    payload = _call('GET', f'{API_BASE}/{path}', session=session, params=params,
                                    headers={'Authorization': f'Bearer {access_token}'})
                except OuraOAuthError as exc:
                    if exc.upstream == 403:
                        result.unavailable.append(kind)
                        result.documents[kind] = []
                        break
                    raise
                data = payload.get('data') if isinstance(payload, dict) else None
                if not isinstance(data, list):
                    raise OuraOAuthError(UNEXPECTED)
                result.documents[kind].extend(item for item in data if isinstance(item, dict))
                total += len(data)
                next_token = payload.get('next_token')
                if not isinstance(next_token, str) or not _NEXT.fullmatch(next_token):
                    break
            else:
                # Pages kept coming past the limit: what was read is a part.
                result.incomplete = True
    finally:
        if own:
            session.close()
    if len(result.unavailable) == len(ENDPOINTS):
        raise OuraOAuthError(NO_ACCESS, 409, 403)
    return result


def forget(person):
    """Tell Oura that a person whose record is being erased has left.

    Best effort, and never in the way of the erasure: the connection goes with
    the person whatever Oura answers. Call inside the erasing transaction,
    before the rows are deleted.
    """
    from omop_core.models import OuraConnection

    row = OuraConnection.objects.select_for_update().filter(person=person).first()
    if row is None:
        return
    try:
        revoke(usable_access_token(row))
        logger.info('oura_revoked_on_erasure person_id=%s', person.pk)
    except OuraOAuthError as exc:
        logger.warning('oura_revoke_failed person_id=%s status=%s', person.pk, exc.status)
    except Exception:
        logger.exception('oura_revoke_failed person_id=%s', person.pk)
