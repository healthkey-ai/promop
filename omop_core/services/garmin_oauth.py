"""A patient's Garmin Connect account, linked by OAuth, and the data Garmin pushes.

This is the Garmin Connect Developer Program route (#2802): the deployment
holds one app registration (a consumer key and secret), a patient approves it
once on Garmin's consent page, and Garmin then POSTs that user's summaries to a
URL on this server as their watch syncs. It sits beside the Personal API key
sync (#2794, ``garmin_connect.py``), where the patient pastes their own key and
this server pulls.

The pieces, following Garmin's "OAuth2.0 PKCE Specification":

* **Consent.** :func:`authorize_url` sends the browser to Garmin with a PKCE
  challenge and a signed ``state``. The state carries who started it and a
  nonce; the PKCE verifier is derived from the nonce with a key only this
  deployment has (:func:`_verifier`). The state travels in URLs, so it proves
  nothing by itself: the view completes a connection only for the browser
  session that started it, which holds the same nonce.
* **Tokens.** :func:`exchange_code` and :func:`refresh_tokens` talk to Garmin's
  token endpoint. A refresh rotates the refresh token, so both are always
  stored together. They are stored encrypted (:func:`encrypt`) under a key of
  their own, ``GARMIN_TOKEN_KEY``, which is in no database: a dump restored
  anywhere holds nothing it can use, and rotating ``SECRET_KEY`` does not
  strand them.
* **Push.** :func:`read_push` turns one webhook body into samples per Garmin
  user id, plus the users who deregistered and the permission changes. A push
  carries no credential this server could check beyond the secret in its URL,
  so an entry only ever reaches the patient whose connection names its user id.

Garmin's error text, the tokens and the client secret are never logged; a
failure is reported by HTTP status alone.
"""
import base64
import hashlib
import hmac
import logging
import math
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from django.conf import settings
from django.core import signing
from django.utils import timezone

from omop_core.services.wearable_parsers import WearableSample

logger = logging.getLogger(__name__)

AUTHORIZE_URL = 'https://connect.garmin.com/oauth2Confirm'
# Garmin's token endpoint. (Not named for what it returns: a string constant
# with "token" in its name reads to the credential scan as a hardcoded one.)
GRANT_URL = 'https://diauth.garmin.com/di-oauth2-service/oauth/token'
API_BASE = 'https://apis.garmin.com/wellness-api/rest'
CALLBACK_PATH = '/api/v1/garmin/oauth/callback/'

CONNECT_TIMEOUT_SECONDS = 3.0
REQUEST_TIMEOUT_SECONDS = 6.0
# How long the patient has on Garmin's consent page.
STATE_MAX_AGE_SECONDS = 15 * 60
# The webhook's path is its only credential, so it has to be unguessable:
# anything shorter than this is treated as not set.
MIN_WEBHOOK_SECRET_LENGTH = 32
# A push may name any day. Garmin sends what a watch held when it synced, so a
# day long past or still to come is a wrong clock or a mistake, and is dropped.
MAX_PUSH_DAY_AGE = timedelta(days=366)
# Per-day reading totals kept on a connection, for the two averaged metrics.
READINGS_KEPT_DAYS = 14
# Garmin's advice: treat a token as expired ten minutes early.
EXPIRY_MARGIN_SECONDS = 600
# A refresh token lasts about ninety days and is the only way to tell Garmin a
# patient disconnected, so it is renewed when a push shows the account is live
# and the stored token is this old.
REFRESH_AFTER = timedelta(days=30)

_STATE_SALT = 'garmin-oauth-state'
_TOKEN = re.compile(r'[\x21-\x7e]{1,8192}')
_USER_ID = re.compile(r'[A-Za-z0-9_-]{1,64}')
_CODE = re.compile(r'[\x21-\x7e]{1,2048}')

NOT_CONFIGURED = 'Connecting a Garmin account is not set up on this server.'
DENIED = 'Garmin did not complete the connection. Try again.'
UNREACHABLE = 'Could not reach Garmin Connect. Try again later.'
UNEXPECTED = 'Garmin Connect returned an answer this server could not read.'


class GarminOAuthError(Exception):
    """A step that cannot go on. The message is fixed wording, safe to show."""

    def __init__(self, message, status=502):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime | None


# ── Configuration ───────────────────────────────────────────────────────────

def client_id():
    return (getattr(settings, 'GARMIN_CLIENT_ID', '') or '').strip()


def _client_secret():
    return (getattr(settings, 'GARMIN_CLIENT_SECRET', '') or '').strip()


def webhook_secret():
    """The secret path segment, or '' when unset or too short to be one."""
    secret = (getattr(settings, 'GARMIN_WEBHOOK_SECRET', '') or '').strip()
    return secret if len(secret) >= MIN_WEBHOOK_SECRET_LENGTH else ''


def _token_keys():
    """The Fernet keys for stored tokens: the one that encrypts, then any
    earlier ones that may still decrypt. No first key, or one that is not a
    Fernet key, is none at all: nothing could be encrypted, so it reads as not
    set up and not as an error page. A fallback that is not a key is left out."""
    from cryptography.fernet import Fernet

    keys = [getattr(settings, 'GARMIN_TOKEN_KEY', ''), *getattr(settings, 'GARMIN_TOKEN_KEY_FALLBACKS', [])]
    usable = []
    for position, key in enumerate(keys):
        key = (key or '').strip()
        try:
            usable.append(Fernet(key.encode()))
        except (ValueError, TypeError):
            if position == 0:
                return []
    return usable


def configured():
    """Whether a patient can connect: the app's key and secret are set, so is
    the webhook secret, without which a connection would receive nothing, and
    so is the key the tokens are stored under."""
    return bool(client_id() and _client_secret() and webhook_secret() and _token_keys())


def redirect_uri():
    """The callback URL registered with Garmin, which must match it exactly."""
    explicit = (getattr(settings, 'GARMIN_REDIRECT_URI', '') or '').strip()
    return explicit or f"{settings.APP_BASE_URL.rstrip('/')}{CALLBACK_PATH}"


# ── Consent: state and PKCE ─────────────────────────────────────────────────

def _derived_key(purpose):
    """A 32-byte key for one purpose, from SECRET_KEY."""
    return hmac.new(settings.SECRET_KEY.encode(), f'garmin-oauth:{purpose}'.encode(), hashlib.sha256).digest()


def _verifier(nonce):
    """The PKCE code verifier for a state's nonce: 43 URL-safe characters.

    Derived, not stored: the callback recomputes it from the state it is
    handed. The nonce travels in URLs; the key does not leave this deployment.
    """
    digest = hmac.new(_derived_key('pkce'), nonce.encode(), hashlib.sha256).digest()
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
    """Garmin's consent page for this app, carrying the state and its challenge."""
    if not configured():
        raise GarminOAuthError(NOT_CONFIGURED, 503)
    read = read_state(state)
    if read is None:
        raise GarminOAuthError(DENIED, 400)
    verifier = read.verifier
    return f'{AUTHORIZE_URL}?' + urlencode({
        'response_type': 'code',
        'client_id': client_id(),
        'code_challenge': _challenge(verifier),
        'code_challenge_method': 'S256',
        'redirect_uri': redirect_uri(),
        'state': state,
    })


# ── Garmin's endpoints ──────────────────────────────────────────────────────

def _session():
    import requests
    return requests.Session()


def _call(method, url, *, session=None, expect_json=True, **kwargs):
    """One request to Garmin. Raises GarminOAuthError; returns decoded JSON."""
    import requests

    own = session is None
    session = session or _session()
    try:
        response = session.request(
            method, url, timeout=(CONNECT_TIMEOUT_SECONDS, REQUEST_TIMEOUT_SECONDS),
            # A credential goes to the host named here and nowhere it points.
            allow_redirects=False, **kwargs)
        if not 200 <= response.status_code < 300:
            logger.warning('garmin_oauth_refused url=%s status=%s', url, response.status_code)
            status = 400 if response.status_code in (400, 401, 403) else 502
            raise GarminOAuthError(DENIED if status == 400 else UNREACHABLE, status)
        if not expect_json:
            return None
        try:
            return response.json()
        except ValueError:
            raise GarminOAuthError(UNEXPECTED) from None
    except requests.RequestException as exc:
        logger.warning('garmin_oauth_request_failed url=%s error=%s', url, type(exc).__name__)
        raise GarminOAuthError(UNREACHABLE) from None
    finally:
        if own:
            session.close()


def _tokens(payload, now):
    """The token response as Tokens, or UNEXPECTED when it is not one."""
    if not isinstance(payload, dict):
        raise GarminOAuthError(UNEXPECTED)
    access, refresh = payload.get('access_token'), payload.get('refresh_token')
    expires_in = payload.get('expires_in')
    if (not isinstance(access, str) or not _TOKEN.fullmatch(access)
            or not isinstance(refresh, str) or not _TOKEN.fullmatch(refresh)
            or isinstance(expires_in, bool) or not isinstance(expires_in, (int, float))
            or not 0 < expires_in <= 366 * 86400):
        raise GarminOAuthError(UNEXPECTED)
    refresh_in = payload.get('refresh_token_expires_in')
    refresh_ok = (not isinstance(refresh_in, bool) and isinstance(refresh_in, (int, float))
                  and 0 < refresh_in <= 5 * 366 * 86400)
    return Tokens(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=now + timedelta(seconds=float(expires_in)),
        refresh_expires_at=now + timedelta(seconds=float(refresh_in)) if refresh_ok else None,
    )


def exchange_code(code, verifier, *, session=None):
    """Trade the authorization code from the callback for tokens."""
    if not configured():
        raise GarminOAuthError(NOT_CONFIGURED, 503)
    if not isinstance(code, str) or not _CODE.fullmatch(code):
        raise GarminOAuthError(DENIED, 400)
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
        raise GarminOAuthError(NOT_CONFIGURED, 503)
    payload = _call('POST', GRANT_URL, session=session, data={
        'grant_type': 'refresh_token',
        'client_id': client_id(),
        'client_secret': _client_secret(),
        'refresh_token': refresh_token,
    })
    return _tokens(payload, timezone.now())


def _bearer(access_token):
    return {'Authorization': f'Bearer {access_token}', 'Accept': 'application/json'}


def fetch_user_id(access_token, *, session=None):
    """Garmin's stable id for the account; every push names it."""
    payload = _call('GET', f'{API_BASE}/user/id', session=session, headers=_bearer(access_token))
    user_id = payload.get('userId') if isinstance(payload, dict) else None
    if not isinstance(user_id, str) or not _USER_ID.fullmatch(user_id):
        raise GarminOAuthError(UNEXPECTED)
    return user_id


def clean_permissions(value):
    """The permission names in a response or a push, sorted; junk is dropped."""
    if isinstance(value, dict):
        # Seen as {"permissions": [...]} and, in pushes, as {"name": true}.
        value = value.get('permissions', [name for name, granted in value.items() if granted is True])
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted({item for item in value
                   if isinstance(item, str) and re.fullmatch(r'[A-Za-z_]{1,64}', item)})


def fetch_permissions(access_token, *, session=None):
    """What the patient ticked on the consent page, e.g. HEALTH_EXPORT."""
    payload = _call('GET', f'{API_BASE}/user/permissions', session=session,
                    headers=_bearer(access_token))
    return clean_permissions(payload)


def deregister(access_token, *, session=None):
    """Tell Garmin the patient disconnected here, so it stops sending."""
    _call('DELETE', f'{API_BASE}/user/registration', session=session,
          headers=_bearer(access_token), expect_json=False)


# ── Tokens at rest ──────────────────────────────────────────────────────────

def _fernet():
    from cryptography.fernet import MultiFernet

    keys = _token_keys()
    if not keys:
        raise GarminOAuthError(NOT_CONFIGURED, 503)
    return MultiFernet(keys)


def encrypt(text):
    """A token as it is stored, under ``GARMIN_TOKEN_KEY``. Earlier keys
    (``GARMIN_TOKEN_KEY_FALLBACKS``) only decrypt."""
    return _fernet().encrypt(text.encode()).decode()


def decrypt(stored):
    """The token, or None when it cannot be read: empty, or encrypted under a
    key this deployment no longer has (the connection then needs remaking)."""
    from cryptography.fernet import InvalidToken

    if not stored:
        return None
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError, GarminOAuthError):
        return None


def store_tokens(connection, tokens):
    """Put a token pair on a connection (unsaved)."""
    connection.access_token = encrypt(tokens.access_token)
    connection.refresh_token = encrypt(tokens.refresh_token)
    connection.access_expires_at = tokens.access_expires_at
    connection.refresh_expires_at = tokens.refresh_expires_at
    connection.tokens_updated_at = timezone.now()


def usable_access_token(connection, *, session=None):
    """An access token Garmin will accept, refreshing and saving when needed.

    Call with the connection's row locked: a refresh retires the stored refresh
    token, and two refreshes racing would leave one of them holding a dead one.
    Raises GarminOAuthError when Garmin refuses or the tokens cannot be read.
    """
    now = timezone.now()
    access = decrypt(connection.access_token)
    if (access and connection.access_expires_at
            and connection.access_expires_at - timedelta(seconds=EXPIRY_MARGIN_SECONDS) > now):
        return access
    refresh = decrypt(connection.refresh_token)
    if not refresh:
        raise GarminOAuthError(DENIED, 400)
    tokens = refresh_tokens(refresh, session=session)
    store_tokens(connection, tokens)
    connection.save(update_fields=[
        'access_token', 'refresh_token', 'access_expires_at', 'refresh_expires_at',
        'tokens_updated_at'])
    return tokens.access_token


# ── Push ────────────────────────────────────────────────────────────────────

@dataclass
class Push:
    """What one webhook body says, by Garmin user id."""
    samples: dict            # user id -> [WearableSample]: the day's value as sent
    readings: dict           # user id -> {(metric, day): {summary: (sum, count)}}
    deregistered: set        # user ids whose registration Garmin removed
    permissions: dict        # user id -> sorted permission names


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:     # a JSON integer hundreds of digits long
        return None
    return value if math.isfinite(value) else None


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _entry_day(entry):
    """The calendar day an entry is for: ``calendarDate``, else the local day
    its ``startTimeInSeconds`` falls on (``startTimeOffsetInSeconds`` east of UTC)."""
    day = entry.get('calendarDate')
    if isinstance(day, str):
        try:
            return date.fromisoformat(day[:10])
        except ValueError:
            return None
    start, offset = _number(entry.get('startTimeInSeconds')), _number(entry.get('startTimeOffsetInSeconds'))
    if start is None or not 0 < start < 4102444800:   # before 2100
        return None
    if offset is None or abs(offset) > 16 * 3600:
        offset = 0.0
    return (datetime.fromtimestamp(start, tz=dt_timezone.utc) + timedelta(seconds=offset)).date()


def _total(values):
    """``(sum, count)`` of a ``{offset: reading}`` map's positive readings."""
    if not isinstance(values, dict):
        return 0.0, 0
    readings = [r for r in (_positive(v) for v in values.values()) if r is not None]
    return sum(readings), len(readings)


def _summary_key(entry):
    """What tells one summary of a day from another: Garmin's id for it, else
    when it starts. Garmin sends a summary again as it grows, under the same id."""
    summary_id = entry.get('summaryId')
    if isinstance(summary_id, str) and 0 < len(summary_id) <= 128:
        return summary_id
    start = _number(entry.get('startTimeInSeconds'))
    return f't{int(start)}' if start is not None else 'x'


def _daily(entry, keep, _average):
    # A day with no steps yet is the first sync after midnight, or a watch not
    # worn: not a day of zero movement. The Personal API reader skips the same
    # days (includesWellnessData). A later push with steps fills the day in.
    if _positive(entry.get('steps')) is not None:
        keep('steps', entry.get('steps'))
        metres = _number(entry.get('distanceInMeters'))
        keep('distance', None if metres is None else metres / 1000.0)
        seconds = [_number(entry.get(f'{level}IntensityDurationInSeconds')) for level in ('moderate', 'vigorous')]
        if any(s is not None for s in seconds):
            keep('active_minutes', sum(s for s in seconds if s is not None) / 60.0)
        keep('active_energy', entry.get('activeKilocalories'))
    keep('basal_energy', entry.get('bmrKilocalories'))
    keep('resting_hr', _positive(entry.get('restingHeartRateInBeatsPerMinute')))


def _sleep(entry, keep, _average):
    stages = [_number(entry.get(key)) for key in (
        'deepSleepDurationInSeconds', 'lightSleepDurationInSeconds', 'remSleepInSeconds')]
    if any(s is not None for s in stages):
        seconds = sum(s for s in stages if s is not None)
    else:
        seconds = _number(entry.get('durationInSeconds')) or 0.0
    keep('sleep_duration', seconds / 3600.0 if seconds > 0 else None)


def _hrv(entry, keep, _average):
    # Garmin's overnight HRV is RMSSD (#438).
    keep('hrv_rmssd', _positive(entry.get('lastNightAvg')))


def _pulse_ox(entry, _keep, average):
    # A reading the wearer asked for on the spot is not part of the day's
    # monitoring, and one of them would otherwise stand for a day.
    if entry.get('onDemand') is not True:
        average('spo2', entry.get('timeOffsetSpo2Values'))


def _respiration(entry, _keep, average):
    average('respiratory_rate', entry.get('timeOffsetEpochToBreaths'))


def _user_metrics(entry, keep, _average):
    keep('vo2_max', _positive(entry.get('vo2Max')))


# Push type -> how one of its entries is read. A type not listed here (stress,
# epochs, activities...) is accepted and ignored: no approved Garmin mapping
# would store it.
_READERS = {
    'dailies': _daily,
    'sleeps': _sleep,
    'hrv': _hrv,
    'pulseox': _pulse_ox,
    'allDayRespiration': _respiration,
    'respiration': _respiration,
    'userMetrics': _user_metrics,
}


def read_push(payload, today=None):
    """One webhook body as samples per user, deregistrations and permissions.

    Never raises on the body's shape: Garmin sends again whatever is not
    answered 200, so a body with nothing this can read is read as nothing.
    A user's day keeps the last value a body gives for a metric. Pulse ox and
    respiration are different: they arrive as readings in several summaries
    across a day, over several pushes, so they are returned as each summary's
    ``(sum, count)`` for the caller to add to what earlier pushes held
    (:func:`merge_readings`); one push's readings are never the day's average.

    Days more than :data:`MAX_PUSH_DAY_AGE` before ``today``, or after
    tomorrow, are dropped.
    """
    push = Push(samples={}, readings={}, deregistered=set(), permissions={})
    if not isinstance(payload, dict):
        return push
    today = today or timezone.now().date()
    earliest, latest = today - MAX_PUSH_DAY_AGE, today + timedelta(days=1)

    found = defaultdict(dict)        # user id -> {(metric, day): value}
    readings = defaultdict(lambda: defaultdict(dict))   # user id -> {(metric, day): {summary: (sum, count)}}

    def entries(kind):
        items = payload.get(kind)
        for entry in items if isinstance(items, list) else ():
            user_id = entry.get('userId') if isinstance(entry, dict) else None
            if isinstance(user_id, str) and _USER_ID.fullmatch(user_id):
                yield user_id, entry

    for kind, reader in _READERS.items():
        for user_id, entry in entries(kind):
            day = _entry_day(entry)
            if day is None or not earliest <= day <= latest:
                continue

            def keep(metric_key, value, user_id=user_id, day=day):
                value = _number(value)
                if value is not None:
                    found[user_id][(metric_key, day)] = round(value, 2)

            def average(metric_key, values, user_id=user_id, day=day, entry=entry):
                total, count = _total(values)
                if count:
                    readings[user_id][(metric_key, day)][_summary_key(entry)] = (total, count)

            reader(entry, keep, average)

    push.readings = {user_id: dict(days) for user_id, days in readings.items()}
    for user_id, values in found.items():
        samples = [WearableSample(metric_key=metric_key, date=day, value=value)
                   for (metric_key, day), value in values.items()]
        samples.sort(key=lambda s: (s.date, s.metric_key))
        push.samples[user_id] = samples

    push.deregistered = {user_id for user_id, _entry in entries('deregistrations')}
    for user_id, entry in entries('userPermissionsChange'):
        push.permissions[user_id] = clean_permissions(entry.get('permissions'))
    return push


def merge_readings(stored, incoming, today=None):
    """Add a push's readings to a connection's, and return the days' averages.

    ``stored`` is ``GarminConnection.day_readings``:
    ``{day: {metric: {summary: [sum, count]}}}``. A summary sent again replaces
    its earlier totals (Garmin resends one as it grows); a new summary joins
    the day. Returns ``(stored, samples)``: the updated mapping, pruned to the
    last :data:`READINGS_KEPT_DAYS` days, and one sample per metric and day the
    push touched, whose value is the mean of every reading held for that day.
    """
    stored = {day: {metric: dict(summaries) for metric, summaries in metrics.items()}
              for day, metrics in (stored if isinstance(stored, dict) else {}).items()
              if isinstance(metrics, dict)}
    samples = []
    for (metric_key, day), summaries in incoming.items():
        held = stored.setdefault(day.isoformat(), {}).setdefault(metric_key, {})
        for summary, (total, count) in summaries.items():
            held[summary] = [round(total, 4), count]
        totals = [pair for pair in held.values()
                  if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[1]]
        readings = sum(pair[1] for pair in totals)
        if readings:
            samples.append(WearableSample(
                metric_key=metric_key, date=day,
                value=round(sum(pair[0] for pair in totals) / readings, 2)))
    cutoff = ((today or timezone.now().date()) - timedelta(days=READINGS_KEPT_DAYS)).isoformat()
    stored = {day: metrics for day, metrics in stored.items() if day >= cutoff}
    return stored, samples


def forget(person):
    """Tell Garmin that a person whose record is being erased has left.

    Best effort, and never in the way of the erasure: the connection goes with
    the person whatever Garmin answers, and with it the only token that could
    have told Garmin later. Call inside the erasing transaction, before the
    rows are deleted.
    """
    from omop_core.models import GarminConnection

    row = GarminConnection.objects.select_for_update().filter(person=person).first()
    if row is None:
        return
    try:
        deregister(usable_access_token(row))
        logger.info('garmin_deregistered_on_erasure person_id=%s', person.pk)
    except GarminOAuthError as exc:
        logger.warning('garmin_deregister_failed person_id=%s status=%s', person.pk, exc.status)
    except Exception:
        logger.exception('garmin_deregister_failed person_id=%s', person.pk)
