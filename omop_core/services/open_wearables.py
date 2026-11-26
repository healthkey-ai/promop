"""Speak the Open Wearables mobile SDK's protocol, without its server (#2810).

Apple HealthKit and Google Health Connect have no cloud API: only an app on
the phone can read them. Open Wearables (github.com/the-momentum/open-wearables)
ships SDKs that do, and they post what they read to fixed paths under
``{host}/api/v1/``. This deployment serves those paths itself, so an app built
on the SDK is pointed at this site and nothing else has to be run.

What the SDK calls, and what answers here (``patient_portal/api/open_wearables.py``):

==============================================  ==================================
``POST /sdk/users/{user_id}/sync``              a batch of records, sleep, workouts
``GET  /sdk/users/{user_id}/sync/status``       where the server says a sync stands
``POST /sdk/users/{user_id}/logs``              the SDK's own diagnostics (dropped)
``POST /token/refresh``                         a new access token
``DELETE /users/{user_id}/connections/{p}``     the user signed out in the app
``POST /invitation-code/redeem``                the app's exchange of a code for tokens
==============================================  ==================================

This module holds the rules, none of the HTTP: the credentials, the reading of
a sync body into per-day readings, and the running day totals.

**Raw records in, day values out.** The SDK sends every HealthKit sample: a
day's steps are hundreds of records in several batches, and a retry or a
re-export sends some twice. The patient's record wants one value per metric
per day, the same one the ``export.zip`` parser computes (a sum for the
cumulative metrics, a mean otherwise: ``CUMULATIVE_METRICS``). So each record
is counted once into ``OpenWearablesDay`` (by a short key of its id, kept
with its value while the day is open) and the day's value is recomputed and
written whenever it changes. No record's time is stored, and its key and
value only until the day is closed.

**Nothing here guesses.** A record type or unit this module does not know is
counted and reported, never stored under a nearby meaning.

Not verified against a live phone: the shapes are read from the SDK's and the
server's source (``open_wearables_ios_sdk``, ``backend/app/schemas/providers/
mobile_sdk``), not from a captured request.
"""
from __future__ import annotations

import hashlib
import math
import re
import secrets
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core import signing
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from omop_core.services.wearable_parsers import (
    CUMULATIVE_METRICS, WearableSample, _APPLE_TYPE_MAP,
)

# ── Limits ──────────────────────────────────────────────────────────────────

#: One sync body, read from the stream (not the form limit). The SDK pages by
#: record and Android sends a heart-rate record as one item per sample, so a
#: page is large; and the SDK sends a refused page again unchanged, for ever,
#: so this has to be above any page it builds.
MAX_SYNC_BYTES = 32 * 1024 * 1024
#: Readings kept from one body. Only what is read counts: the heart-rate
#: samples and workouts that are skipped cost nothing and are not limited. No
#: page an SDK builds reaches it (a page is a few thousand records).
MAX_ITEMS = 50000
#: Record keys kept for one metric on one day. A day of steps is hundreds of
#: records, of active energy a couple of thousand.
MAX_SEEN_PER_DAY = 6000
#: A day nothing has arrived for in this long is closed (``close_idle_days``).
CLOSE_AFTER = timedelta(days=45)
#: ... and sooner for a day already this far in the past: a backfill sends an
#: old day's records together, so its keys are not kept for weeks.
SETTLED_AFTER = timedelta(days=7)
CLOSE_SETTLED_AFTER = timedelta(days=1)
#: HealthKit arrived with iOS 8 in 2014; nothing real is older.
EARLIEST_DAY = date(2014, 1, 1)

ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_LIFETIME = timedelta(days=180)
CODE_LIFETIME = timedelta(minutes=15)
CODE_LENGTH = 8
#: Open Wearables' own alphabet: no 0 or O, no 1 or I.
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
MAX_DEVICES = 5

_ACCESS_SALT = 'open-wearables-access'
_CODE_SALT = 'open-wearables-code'
_REFRESH_PREFIX = 'owr_'
_CODE_RE = re.compile(rf'[{CODE_ALPHABET}]{{{CODE_LENGTH}}}')
_REFRESH_RE = re.compile(r'owr_[A-Za-z0-9_-]{40,128}')
_ACCESS_RE = re.compile(r'[\x21-\x7e]{20,1024}')

# ── Providers and record types ──────────────────────────────────────────────

APPLE, ANDROID = 'apple', 'android'

#: The SDK's ``provider`` -> the wearable writer's device type. ``google`` is
#: what Health Connect was called before Open Wearables split the provider, and
#: deployed apps still send it. The Android SDK reads one store, never both,
#: and on a Galaxy phone that is Samsung Health unless the app chose Health
#: Connect: same record names, same units, so it is read the same way.
PROVIDER_DEVICE = {'apple': APPLE, 'health_connect': ANDROID, 'google': ANDROID, 'samsung': ANDROID}
PROVIDER_SLUGS = {'apple': 'apple', 'health_connect': 'health_connect',
                  'google': 'health_connect', 'samsung': 'samsung'}

#: History rows of an unattended sync are named ``<label> <YYYY-MM>``.
HISTORY_LABELS = {APPLE: 'Open Wearables sync (iPhone)', ANDROID: 'Open Wearables sync (Android)'}

# Health Connect record types as the Android SDK names them -> metric key.
# HEART_RATE_VARIABILITY is RMSSD in Health Connect, never SDNN (#438).
# DISTANCE is left out: a Health Connect distance is walking, running and
# cycling together, which is not the walking-and-running metric.
_ANDROID_TYPE_MAP = {
    'STEP_COUNT': 'steps',
    'RESTING_HEART_RATE': 'resting_hr',
    'HEART_RATE_VARIABILITY': 'hrv_rmssd',
    'OXYGEN_SATURATION': 'spo2',
    'RESPIRATORY_RATE': 'respiratory_rate',
    'VO2_MAX': 'vo2_max',
    'FLOORS_CLIMBED': 'flights_climbed',
    'ACTIVE_CALORIES_BURNED': 'active_energy',
    'WEIGHT': 'body_mass',
}

_TYPE_MAPS = {APPLE: _APPLE_TYPE_MAP, ANDROID: _ANDROID_TYPE_MAP}

#: Sleep stages that are time asleep (Open Wearables' ``ASLEEP_STAGES``).
ASLEEP_STAGES = frozenset({'sleeping', 'light', 'deep', 'rem'})

# Units, as factors to the unit the stored row is in (the writer's unit_map).
# Only metrics that arrive in more than one unit are listed: for those an
# unknown unit is refused, because a mile stored as a kilometre is a wrong
# fact. Compared as sent: "Cal" is the kilocalorie HealthKit reports and "cal"
# is a thousandth of it.
_UNIT_FACTORS = {
    'distance': {'km': 1.0, 'm': 0.001, 'mi': 1.609344},
    'walking_speed': {'km/hr': 1.0, 'km/h': 1.0, 'm/s': 3.6, 'mi/hr': 1.609344, 'mph': 1.609344},
    'walking_step_length': {'cm': 1.0, 'm': 100.0, 'in': 2.54},
    'active_energy': {'kcal': 1.0, 'Cal': 1.0, 'kJ': 1 / 4.184},
    'basal_energy': {'kcal': 1.0, 'Cal': 1.0, 'kJ': 1 / 4.184},
    'body_mass': {'kg': 1.0, 'g': 0.001, 'lb': 0.45359237, 'lbs': 0.45359237},
    'active_minutes': {'min': 1.0, 'h': 60.0, 'hr': 60.0, 's': 1 / 60},
}
# The SDK sends "%" on a 0-100 scale; a 0-1 fraction is HealthKit's own.
_PERCENT_METRICS = frozenset({'spo2', 'walking_double_support_pct'})

_OFFSET_RE = re.compile(r'([+-])(\d{2}):?(\d{2})')
_MAX_NAMES = 20
_MAX_NAME_CHARS = 64


def enabled():
    """Whether this deployment answers the SDK at all."""
    return bool(getattr(settings, 'OPEN_WEARABLES_SYNC_ENABLED', True))


# ── Credentials ─────────────────────────────────────────────────────────────

def _digest(salt, value):
    """A keyed digest: a database dump holds nothing that can be presented."""
    return salted_hmac(salt, value, algorithm='sha256').hexdigest()


def new_code():
    """A connection code, as the patient reads it and as it is stored."""
    code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return code, _digest(_CODE_SALT, code)


def code_digest(code):
    """The stored form of what an app sent as a code, or None when it cannot be one."""
    if not isinstance(code, str):
        return None
    code = code.strip().upper()
    return _digest(_CODE_SALT, code) if _CODE_RE.fullmatch(code) else None


def new_refresh_token():
    """A refresh token, as the phone holds it and as it is stored."""
    token = _REFRESH_PREFIX + secrets.token_urlsafe(48)
    return token, refresh_digest(token)


def refresh_digest(token):
    if not isinstance(token, str) or not _REFRESH_RE.fullmatch(token):
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def access_token(device):
    """A short-lived token naming one signed-in phone. Good only on the SDK routes."""
    return signing.dumps({'u': str(device.connection.user_id), 'd': device.pk}, salt=_ACCESS_SALT)


def read_access_token(token):
    """``(user_id, device_id)`` of a token this deployment issued and that has
    not expired, else None."""
    if not isinstance(token, str) or not _ACCESS_RE.fullmatch(token):
        return None
    try:
        claims = signing.loads(token, salt=_ACCESS_SALT, max_age=ACCESS_TOKEN_SECONDS)
        return uuid.UUID(claims['u']), int(claims['d'])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None


def same_user(user_id, path_user_id):
    return constant_time_compare(str(user_id), str(path_user_id))


def clean_time_zone(name):
    """An IANA zone name the patient's browser reported, or ''."""
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_+\-/]{1,64}', name):
        return ''
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ''
    return name


# ── Reading a sync body ─────────────────────────────────────────────────────

class Reading(NamedTuple):
    metric_key: str
    day: date
    key: str        # short digest of the record's identity
    value: float


class Batch(NamedTuple):
    readings: list
    #: Record types this module does not read, as the phone sent them.
    unrecognized: list
    #: Records dropped: no usable time or value, or a unit that cannot be converted.
    rejected: int
    #: The newest instant any record in the body ends at, or None.
    newest: datetime | None


class SyncError(Exception):
    """A body that cannot be read at all. ``message`` is fixed wording."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _instant(value):
    """An aware datetime from the SDK's ISO 8601 text, or None."""
    if not isinstance(value, str) or not 10 <= len(value) <= 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=dt_timezone.utc)


def _offset(value):
    """A ``+01:00`` offset as a timedelta, or None."""
    if isinstance(value, str) and value.strip().upper() == 'Z':
        return timedelta(0)         # how Android writes UTC
    match = _OFFSET_RE.fullmatch(value.strip()) if isinstance(value, str) else None
    if match is None:
        return None
    hours, minutes = int(match.group(2)), int(match.group(3))
    if hours > 14 or minutes > 59:
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return -delta if match.group(1) == '-' else delta


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value or len(value) > 32:
            return None
    elif not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _name(value):
    if not isinstance(value, str) or not value.strip():
        return '(no type)'
    return ''.join(ch for ch in value if ch.isprintable())[:_MAX_NAME_CHARS]


class _Reader:
    def __init__(self, device_type, zone, today):
        self.types = _TYPE_MAPS[device_type]
        self.device_type = device_type
        self.zone = zone
        # A day ahead of the server's date: the phone may be in a later zone.
        self.last_day = today + timedelta(days=1)
        # Instants outside these cannot belong to a day that is kept, and
        # arithmetic on the far ones (year 1, year 9999) overflows.
        self.earliest = datetime.combine(EARLIEST_DAY - timedelta(days=1), datetime.min.time(), dt_timezone.utc)
        self.latest = datetime.combine(self.last_day + timedelta(days=1), datetime.min.time(), dt_timezone.utc)
        self.readings = []
        self.unrecognized = set()
        self.rejected = 0
        self.newest = None

    def _keep(self, reading):
        if len(self.readings) >= MAX_ITEMS:
            raise SyncError(f'Too many records in one request. The limit is {MAX_ITEMS}.', status=413)
        self.readings.append(reading)

    def _day(self, start, record):
        """The calendar day a record belongs to, where the person was.

        The record's own offset when HealthKit kept one (it rarely does), else
        the zone the patient's browser reported when the phone was connected,
        else UTC.
        """
        if not self.earliest <= start <= self.latest:
            return None
        offset = _offset(record.get('zoneOffset'))
        if offset is not None:
            local = start.astimezone(dt_timezone(offset))
        elif self.zone is not None:
            local = start.astimezone(self.zone)
        else:
            local = start.astimezone(dt_timezone.utc)
        day = local.date()
        return day if EARLIEST_DAY <= day <= self.last_day else None

    def _key(self, record, kind, start, end, value):
        """What makes a record the same record when it is sent again."""
        identity = record.get('id')
        if not isinstance(identity, str) or not identity.strip():
            identity = f'{kind}|{start.isoformat()}|{end.isoformat() if end else ""}|{value!r}'
        # surrogatepass: a lone surrogate from a JSON escape is still an id.
        return hashlib.sha256(identity.strip().encode('utf-8', 'surrogatepass')).hexdigest()[:16]

    def _seen_time(self, moment):
        # Bounded like the day: HealthKit's "distant future" as an end date
        # would otherwise tell every phone it is synced until the year 4001.
        if moment is None or not self.earliest <= moment <= self.latest:
            return
        if self.newest is None or moment > self.newest:
            self.newest = moment

    def read(self, read_one, item):
        """One item, whatever is wrong with it: a record that cannot be read
        is counted and the rest of the batch is kept. An item that raised
        would fail the request, and the SDK sends a failed batch again."""
        try:
            read_one(item)
        except SyncError:
            raise
        except Exception:       # noqa: BLE001 - any one record, never the batch
            self.rejected += 1

    def record(self, record):
        if not isinstance(record, dict):
            self.rejected += 1
            return
        kind = record.get('type')
        metric_key = self.types.get(kind) if isinstance(kind, str) else None
        if metric_key is None:
            self.unrecognized.add(_name(kind))
            return
        start, end = _instant(record.get('startDate')), _instant(record.get('endDate'))
        if start is None:
            self.rejected += 1
            return
        if not self.earliest <= start <= self.latest:
            self.rejected += 1
            return
        number = _number(record.get('value'))
        factors = _UNIT_FACTORS.get(metric_key)
        if number is not None and factors is not None:
            unit = record.get('unit')
            factor = factors.get(unit.strip()) if isinstance(unit, str) else None
            number = None if factor is None else number * factor
        if number is None:
            self.rejected += 1
            return
        if metric_key in _PERCENT_METRICS and 0 < number <= 1.0:
            number *= 100.0
        day = self._day(start, record)
        if day is None:
            self.rejected += 1
            return
        self._seen_time(end or start)
        self._keep(Reading(metric_key, day, self._key(record, kind, start, end, number), number))

    def sleep(self, record):
        if not isinstance(record, dict):
            self.rejected += 1
            return
        stage = record.get('stage')
        if not isinstance(stage, str) or stage.strip().lower() not in ASLEEP_STAGES:
            # In bed and awake are not sleep; neither stored nor an error.
            return
        start, end = _instant(record.get('startDate')), _instant(record.get('endDate'))
        if (start is None or end is None or not self.earliest <= start <= self.latest
                or not start < end <= start + timedelta(days=1)):
            self.rejected += 1
            return
        day = self._day(start, record)
        if day is None:
            self.rejected += 1
            return
        hours = (end - start).total_seconds() / 3600.0
        self._seen_time(end)
        self._keep(Reading('sleep_duration', day, self._key(record, 'sleep', start, end, hours), hours))


def read_sync(body, device_type, *, time_zone='', today=None):
    """Turn a sync body into per-day readings.

    ``body`` is the SDK's ``SyncRequest`` as parsed JSON. Workouts are not
    read: the patient's record holds daily values, and a workout's steps and
    energy are in the day's records already. Raises ``SyncError`` for a body
    with no ``data`` object, or one that holds more readings than ``MAX_ITEMS``.
    """
    data = body.get('data') if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise SyncError('Send a JSON object with "provider" and "data".')
    try:
        zone = ZoneInfo(time_zone) if time_zone else None
    except (ZoneInfoNotFoundError, ValueError, OSError):
        zone = None
    reader = _Reader(device_type, zone, today or timezone.now().date())
    for name, read in (('records', reader.record), ('sleep', reader.sleep)):
        items = data.get(name)
        if items is None:
            continue
        if not isinstance(items, list):
            raise SyncError(f'"data.{name}" must be a list.')
        for item in items:
            reader.read(read, item)
    return Batch(
        readings=reader.readings,
        unrecognized=sorted(reader.unrecognized)[:_MAX_NAMES],
        rejected=reader.rejected,
        newest=reader.newest,
    )


# ── Day totals ──────────────────────────────────────────────────────────────

class Applied(NamedTuple):
    #: The day values that changed, for the wearable writer.
    samples: list
    counted: int        # records counted for the first time, or again at a new value
    repeated: int       # records already counted: a retry or a re-export
    late: int           # records for a day that is closed, or already full


def apply_readings(connection, device_type, readings, now=None):
    """Count each reading once into its day and return the days that changed.

    Call inside a transaction holding the patient's lock: two batches for one
    day would otherwise each read the old total.
    """
    from omop_core.models import OpenWearablesDay

    now = now or timezone.now()
    grouped = defaultdict(list)
    for reading in readings:
        grouped[reading.metric_key, reading.day].append(reading)
    if not grouped:
        return Applied([], 0, 0, 0)

    stored = {
        (row.metric_key, row.day): row
        for row in OpenWearablesDay.objects.filter(
            connection=connection, device_type=device_type,
            metric_key__in={metric for metric, _day in grouped},
            day__in={day for _metric, day in grouped},
        )
    }
    created, updated, samples = [], [], []
    counted = repeated = late = 0
    for (metric_key, day), items in grouped.items():
        row = stored.get((metric_key, day))
        if row is None:
            row = OpenWearablesDay(connection=connection, device_type=device_type,
                                   metric_key=metric_key, day=day, seen={})
            is_new = True
        else:
            is_new = False
        if row.closed:
            late += len(items)
            continue
        seen = dict(row.seen) if isinstance(row.seen, dict) else {}
        added = 0
        for reading in items:
            value = round(reading.value, 4)
            if reading.key in seen:
                if seen[reading.key] == value:
                    repeated += 1
                    continue
                # The same record at another value: Health Connect lets a
                # writer update a record in place (a steps bucket that grows,
                # a night re-staged once the watch has synced). The day moves
                # by the difference. HealthKit samples never change.
                row.total += value - seen[reading.key]
            elif len(seen) >= MAX_SEEN_PER_DAY:
                late += 1
                continue
            else:
                row.total += value
                row.count += 1
            seen[reading.key] = value
            added += 1
        if not added:
            continue
        counted += added
        row.seen = seen
        row.updated_at = now
        (created if is_new else updated).append(row)
        value = row.total if metric_key in CUMULATIVE_METRICS else row.total / row.count
        samples.append(WearableSample(metric_key=metric_key, date=day, value=round(value, 2)))
    if created:
        OpenWearablesDay.objects.bulk_create(created)
    if updated:
        OpenWearablesDay.objects.bulk_update(updated, ['total', 'count', 'seen', 'updated_at'])
    samples.sort(key=lambda sample: (sample.metric_key, sample.date))
    return Applied(samples, counted, repeated, late)


def close_idle_days(connection=None, now=None):
    """Drop the record keys of days nothing more is expected for.

    A day nothing has arrived for in ``CLOSE_AFTER``, or, for a day already
    ``SETTLED_AFTER`` in the past, in ``CLOSE_SETTLED_AFTER``: a backfill sends
    an old day's records together. A closed day keeps its total and ignores
    later records: without the keys a record could not be told from one
    already counted. A larger figure for such a day can still arrive in a
    file (the writer keeps the larger of the two for a summed metric).

    Call it after ``apply_readings``, never before: a day this batch has just
    added to is then not idle. With no ``connection``, every connection's
    (``manage.py close_open_wearables_days``, for phones that never sync again).
    """
    from django.db.models import Q

    from omop_core.models import OpenWearablesDay

    now = now or timezone.now()
    days = OpenWearablesDay.objects.filter(closed=False)
    if connection is not None:
        days = days.filter(connection=connection)
    return days.filter(
        Q(updated_at__lt=now - CLOSE_AFTER)
        | Q(day__lt=now.date() - SETTLED_AFTER, updated_at__lt=now - CLOSE_SETTLED_AFTER)
    ).update(closed=True, seen={})
