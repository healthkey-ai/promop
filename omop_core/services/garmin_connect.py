"""Read a patient's daily wearable metrics from the Garmin Connect Personal API.

Garmin lets any Garmin Connect user create a read-only API key for their own
account (Garmin Connect web: Settings, Account, Personal API Token) and
publishes a Python client for it, ``garmin-connect-personal-api-python``. This
module turns one such key and a date range into the same
:class:`~omop_core.services.wearable_parsers.WearableSample` list the ``.fit``
parser returns, so the Wearables upload writes both the same way (#2794).

Three rules hold for the key, which is a credential for the patient's whole
Garmin account:

* **It is used and dropped.** Nothing here stores it, and it is never passed
  to a log call, an exception message or a queued task.
* **The bearer token it buys never touches disk.** The client's own
  ``PersonalApiAuthenticator`` caches tokens in a plaintext file with no way to
  turn that off, which suits one person's laptop and not a server holding many
  patients' tokens. :func:`_exchange_key` makes the same single request and
  keeps the token in memory.
* **Garmin's error text is not relayed.** A failure is reported by HTTP status
  alone; a response body from a third party is not something to copy into a
  log or show a patient.

The sync runs inside the request (a queued task would put the key in the
broker), so it is bounded: at most :data:`MAX_SYNC_DAYS` days, each request
capped at :data:`REQUEST_TIMEOUT_SECONDS`, and the whole read at
:data:`SYNC_BUDGET_SECONDS`, under gunicorn's 30 seconds.
"""
import logging
import math
import re
import time
from datetime import date, timedelta

from omop_core.services.wearable_parsers import WearableSample

logger = logging.getLogger(__name__)

DEFAULT_SYNC_DAYS = 30
MAX_SYNC_DAYS = 90
REQUEST_TIMEOUT_SECONDS = 8.0
SYNC_BUDGET_SECONDS = 22.0

# The shortest range limit the API documents is 28 days (steps and stress);
# sleep allows 31 and the HRV and VO2 max endpoints state none. One chunk size
# for every ranged call stays inside all of them.
CHUNK_DAYS = 28

MAX_API_KEY_LENGTH = 4096
# A key is sent as an HTTP header value: visible ASCII only, so nothing in it
# can end the header or carry a second one.
_API_KEY = re.compile(r'[\x21-\x7e]+')

KEY_REJECTED = ('Garmin did not accept this API key. Check that all of it was '
                'copied and that it has not expired, or create a new key in '
                'Garmin Connect.')
RATE_LIMITED = 'Garmin is limiting requests for this key. Try again later.'
UNREACHABLE = 'Could not reach Garmin Connect. Try again later.'
TOO_SLOW = 'Garmin Connect took too long to answer. Try a shorter range.'
UNEXPECTED = 'Garmin Connect returned an answer this server could not read.'
NOT_INSTALLED = 'Garmin Connect sync is not installed on this server.'


class GarminSyncError(Exception):
    """A sync that cannot go on. ``status`` is the HTTP status to answer with.

    The message is fixed wording written here, safe to show the patient. It
    never contains the key, the token or anything Garmin sent.
    """

    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def clean_api_key(raw):
    """The key as it will be sent, or a 400 :class:`GarminSyncError`."""
    if not isinstance(raw, str) or not raw.strip():
        raise GarminSyncError('Enter your Garmin Connect Personal API key.', 400)
    key = raw.strip()
    if len(key) > MAX_API_KEY_LENGTH or not _API_KEY.fullmatch(key):
        raise GarminSyncError('That does not look like a Garmin Connect Personal API key.', 400)
    return key


def clean_range(days, end_date, today):
    """``(start, end)`` for a request's ``days`` and optional ``end_date``.

    ``end_date`` defaults to yesterday: today's summary is still filling in.
    A caller in a time zone ahead of the server may name tomorrow's date at
    most, which is that caller's today.
    """
    if days in (None, ''):
        days = DEFAULT_SYNC_DAYS
    if isinstance(days, bool) or not isinstance(days, (int, str)):
        raise GarminSyncError(f'days must be a whole number from 1 to {MAX_SYNC_DAYS}.', 400)
    try:
        days = int(days)
    except ValueError:
        days = 0
    if not 1 <= days <= MAX_SYNC_DAYS:
        raise GarminSyncError(f'days must be a whole number from 1 to {MAX_SYNC_DAYS}.', 400)

    if end_date in (None, ''):
        end = today - timedelta(days=1)
    else:
        try:
            end = date.fromisoformat(end_date) if isinstance(end_date, str) else None
        except ValueError:
            end = None
        if end is None:
            raise GarminSyncError('end_date must be a date, YYYY-MM-DD.', 400)
        if end > today + timedelta(days=1):
            raise GarminSyncError('end_date cannot be in the future.', 400)
    return end - timedelta(days=days - 1), end


def fetch_daily_samples(api_key, start, end, *, session=None, clock=time.monotonic):
    """Daily samples for ``start`` to ``end`` inclusive, and what could not be read.

    Returns ``(samples, failed)``: one sample per metric and day, and the names
    of the optional data sets (``'hrv'``, ``'sleep'``, ``'vo2_max'``) Garmin did
    not return. The daily summaries are the body of the sync, so a failure there
    raises; so does a rejected key or a rate limit from any call, because
    carrying on would only repeat it.
    """
    try:
        import requests
        from garmin_personal_api import GarminPersonalApiClient, HttpError
        from garmin_personal_api.constants import TOKEN_PATH
    except ImportError as exc:
        raise GarminSyncError(NOT_INSTALLED, 503) from exc

    deadline = clock() + SYNC_BUDGET_SECONDS

    def timeout():
        """Seconds the next request may take; raises once the budget is spent."""
        remaining = deadline - clock()
        if remaining <= 0.5:
            raise GarminSyncError(TOO_SLOW, 504)
        return min(REQUEST_TIMEOUT_SECONDS, remaining)

    def refuse(status_code):
        """The error for a response status that ends the sync."""
        if status_code in (401, 403):
            return GarminSyncError(KEY_REJECTED, 400)
        if status_code == 429:
            return GarminSyncError(RATE_LIMITED, 429)
        return GarminSyncError(UNREACHABLE, 502)

    own_session = session is None
    session = session or requests.Session()
    try:
        client = GarminPersonalApiClient(
            _exchange_key(session, api_key, TOKEN_PATH, timeout(), refuse),
            timeout=REQUEST_TIMEOUT_SECONDS, session=session,
        )

        def read(call, *args):
            client.timeout = timeout()
            try:
                return call(*args)
            except HttpError as exc:
                logger.warning('garmin_sync_http_error call=%s status=%s',
                               call.__name__, exc.status_code)
                raise refuse(exc.status_code) from None
            except requests.Timeout:
                raise GarminSyncError(TOO_SLOW, 504) from None
            except requests.RequestException as exc:
                logger.warning('garmin_sync_request_failed call=%s error=%s',
                               call.__name__, type(exc).__name__)
                raise GarminSyncError(UNREACHABLE, 502) from None
            except ValueError:
                # A 200 whose body is not JSON.
                raise GarminSyncError(UNEXPECTED, 502) from None

        found = {}

        def keep(metric_key, day, value):
            day, value = _day(day), _number(value)
            if day is not None and value is not None and start <= day <= end:
                found[(metric_key, day)] = round(value, 2)

        _read_summaries(read(client.get_daily_summaries, start, end), keep)

        failed = []
        optional = (
            ('hrv', client.get_daily_hrv_summaries, _read_hrv),
            ('sleep', client.get_daily_sleep_summaries, _read_sleep),
            ('vo2_max', client.get_daily_vo2_max, _read_vo2_max),
        )
        for name, call, parse in optional:
            try:
                for chunk_start, chunk_end in _chunks(start, end):
                    parse(read(call, chunk_start, chunk_end), keep)
            except GarminSyncError as exc:
                if exc.status in (400, 429):
                    raise
                failed.append(name)
    finally:
        if own_session:
            session.close()

    samples = [WearableSample(metric_key=metric_key, date=day, value=value)
               for (metric_key, day), value in found.items()]
    samples.sort(key=lambda s: (s.date, s.metric_key))
    return samples, failed


def _exchange_key(session, api_key, token_path, timeout, refuse):
    """Trade the key for a bearer token, held in memory only."""
    import os

    import requests
    from garmin_personal_api.constants import BASE_URL_ENV_VAR, DEFAULT_BASE_URL

    # The same base the client reads, so the token and the data calls agree.
    base_url = os.environ.get(BASE_URL_ENV_VAR, DEFAULT_BASE_URL).rstrip('/')
    try:
        response = session.post(
            f'{base_url}{token_path}',
            headers={'Accept': '*/*', 'Authorization': f'ApiKey {api_key}'},
            timeout=timeout,
            # The key goes to Garmin's token endpoint and nowhere it points.
            allow_redirects=False,
        )
    except requests.Timeout:
        raise GarminSyncError(TOO_SLOW, 504) from None
    except requests.RequestException as exc:
        logger.warning('garmin_sync_token_request_failed error=%s', type(exc).__name__)
        raise GarminSyncError(UNREACHABLE, 502) from None
    if response.status_code != 200:
        logger.warning('garmin_sync_token_refused status=%s', response.status_code)
        raise refuse(response.status_code)
    token = (response.text or '').strip()
    if not token or len(token) > 16384 or not _API_KEY.fullmatch(token):
        raise GarminSyncError(UNEXPECTED, 502)
    return token


def _chunks(start, end):
    """``start`` to ``end`` in consecutive ranges of at most CHUNK_DAYS days."""
    while start <= end:
        chunk_end = min(end, start + timedelta(days=CHUNK_DAYS - 1))
        yield start, chunk_end
        start = chunk_end + timedelta(days=1)


def _number(value):
    """A finite float, or None. JSON booleans are not measurements."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _day(value):
    """The calendar date in ``value`` (``YYYY-MM-DD``, a time allowed after it)."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _section(record, *path):
    """``record[path[0]][path[1]]...``, or None when any step is missing."""
    for key in path:
        record = record.get(key) if isinstance(record, dict) else None
    return record


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _read_summaries(payload, keep):
    """``/usersummary-service/v2/dailysummaries``: ``{"data": [one per day]}``."""
    records = payload.get('data') if isinstance(payload, dict) else None
    for record in records if isinstance(records, list) else ():
        if not isinstance(record, dict):
            continue
        # A day the watch recorded no all-day data for is not a day of zero
        # steps. Activity-only days hold a partial count, so they are left out
        # as well.
        if record.get('includesWellnessData') is False:
            continue
        day = record.get('calendarDate')
        keep('steps', day, _section(record, 'movement', 'steps', 'value'))
        metres = _number(_section(record, 'movement', 'steps', 'distanceInMeters'))
        keep('distance', day, None if metres is None else metres / 1000.0)
        minutes = [_number(_section(record, 'intensityMinutes', level))
                   for level in ('moderate', 'vigorous')]
        if any(m is not None for m in minutes):
            keep('active_minutes', day, sum(m for m in minutes if m is not None))
        keep('active_energy', day, _section(record, 'calories', 'burnedActive'))
        keep('basal_energy', day, _section(record, 'calories', 'burnedResting'))
        keep('resting_hr', day, _positive(_section(record, 'heartRate', 'restingValue')))
        keep('respiratory_rate', day, _positive(_section(record, 'respiration', 'avgValue')))
        keep('spo2', day, _positive(_section(record, 'pulseOx', 'avgValue')))


def _read_hrv(payload, keep):
    """``/hrv-service/hrv/daily``: overnight HRV, which Garmin computes as RMSSD (#438)."""
    records = payload.get('hrvSummaries') if isinstance(payload, dict) else None
    for record in records if isinstance(records, list) else ():
        if isinstance(record, dict):
            keep('hrv_rmssd', record.get('calendarDate'), _positive(record.get('lastNightAvg')))


def _read_sleep(payload, keep):
    """``/sleep-service/sleep/dailySleepSummariesByDate``: the night's sleep, naps apart."""
    for record in payload if isinstance(payload, list) else ():
        if not isinstance(record, dict):
            continue
        seconds = _positive(record.get('sleepTimeSeconds'))
        keep('sleep_duration', record.get('calendarDate'),
             None if seconds is None else seconds / 3600.0)


def _read_vo2_max(payload, keep):
    """``/metrics-service/metrics/maxmet/daily``: running/walking first, then cycling."""
    for record in payload if isinstance(payload, list) else ():
        for sport in ('generic', 'cycling'):
            entry = _section(record, sport)
            if not isinstance(entry, dict):
                continue
            value = _positive(entry.get('vo2MaxPreciseValue')) or _positive(entry.get('vo2MaxValue'))
            if value is not None:
                keep('vo2_max', entry.get('calendarDate'), value)
                break
