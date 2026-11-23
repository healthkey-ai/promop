"""Read Apple Health daily values posted to the sync endpoint (#2800).

Apple has no cloud API for HealthKit: the data is on the iPhone and only an app
there can read it. So the Apple "API route" is a push. Something on the phone
(an export app, an iOS Shortcut, a native app) posts daily values to
``POST /api/v1/patient-records/sync-apple-health/`` and this module turns the
body into the ``WearableSample``s the ``export.zip`` parser produces.

Two body shapes are read:

* promop's own, keyed by the HealthKit identifier the approved ``Apple`` code
  mappings are keyed by (``unit`` is required for a metric HealthKit exports
  in more than one, ``_UNIT_FACTORS``)::

      {"samples": [{"type": "HKQuantityTypeIdentifierStepCount",
                    "date": "2026-11-22", "value": 8123}]}

* the JSON the Health Auto Export app posts from a REST API automation::

      {"data": {"metrics": [{"name": "step_count", "units": "count",
                             "data": [{"date": "2026-11-22 00:00:00 -0700",
                                       "qty": 8123}]}]}}

**A request carries whole days.** Values for one metric and day are combined as
the ``export.zip`` parser combines a day's records (added up for a cumulative
metric, averaged otherwise), and the endpoint then replaces what earlier Apple
uploads stored for that metric and day. A request holding part of a day
therefore replaces the day with that part.

Nothing here guesses. A metric name or unit this module does not know is
reported back by name and its values are not stored.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import NamedTuple

from omop_core.services.wearable_parsers import (
    CUMULATIVE_METRICS, WearableSample, _APPLE_SLEEP_TYPE, _APPLE_TYPE_MAP,
)

#: Data points read from one request. A year of daily values for every metric
#: is about 6,500; the room above that is for a client that sends a day as
#: several readings.
MAX_POINTS = 10000

#: Request body ceiling, checked from Content-Length before the body is parsed.
#: Under Django's own DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB by default), which
#: DRF's JSON parser also meets, so this is the limit a client is told about.
MAX_BODY_BYTES = 2 * 1024 * 1024

#: Fixed wording for a body that is not JSON of either shape.
UNREADABLE = 'Send a JSON object with "samples" or "data".'

#: HealthKit arrived with iOS 8 in 2014; nothing real is older.
EARLIEST_DAY = date(2014, 1, 1)

#: How many unknown names a response lists, and how much of each.
_MAX_NAMES = 20
_MAX_NAME_CHARS = 64


class AppleHealthPayloadError(Exception):
    """A body that cannot be read at all. ``message`` is fixed wording."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


class ParsedPayload(NamedTuple):
    samples: list[WearableSample]
    #: Metric names (either shape) this module does not read, as the client sent them.
    unrecognized: list[str]
    #: Data points dropped: no usable date or value, or a unit that cannot be converted.
    rejected: int


# HealthKit identifier -> metric key: the quantity types plus sleep, which is a
# category type. The same table resolve_wearable_mappings reverses.
_TYPE_TO_METRIC = {**_APPLE_TYPE_MAP, _APPLE_SLEEP_TYPE: 'sleep_duration'}

# Health Auto Export names a metric by its Health app name in snake case.
_HAE_NAME_TO_TYPE = {
    'step_count': 'HKQuantityTypeIdentifierStepCount',
    'apple_exercise_time': 'HKQuantityTypeIdentifierAppleExerciseTime',
    'resting_heart_rate': 'HKQuantityTypeIdentifierRestingHeartRate',
    'heart_rate_variability': 'HKQuantityTypeIdentifierHeartRateVariabilitySDNN',
    'blood_oxygen_saturation': 'HKQuantityTypeIdentifierOxygenSaturation',
    'respiratory_rate': 'HKQuantityTypeIdentifierRespiratoryRate',
    'vo2_max': 'HKQuantityTypeIdentifierVO2Max',
    'walking_running_distance': 'HKQuantityTypeIdentifierDistanceWalkingRunning',
    'walking_speed': 'HKQuantityTypeIdentifierWalkingSpeed',
    'walking_step_length': 'HKQuantityTypeIdentifierWalkingStepLength',
    'walking_double_support_percentage':
        'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage',
    'walking_heart_rate_average': 'HKQuantityTypeIdentifierWalkingHeartRateAverage',
    'flights_climbed': 'HKQuantityTypeIdentifierFlightsClimbed',
    'active_energy': 'HKQuantityTypeIdentifierActiveEnergyBurned',
    'active_energy_burned': 'HKQuantityTypeIdentifierActiveEnergyBurned',
    'basal_energy_burned': 'HKQuantityTypeIdentifierBasalEnergyBurned',
    'weight_body_mass': 'HKQuantityTypeIdentifierBodyMass',
    'body_mass': 'HKQuantityTypeIdentifierBodyMass',
    'sleep_analysis': _APPLE_SLEEP_TYPE,
}

# Units, as factors to the unit the stored row is in (the writer's unit_map).
# Only metrics that are exported in more than one unit are listed: for those a
# missing or unknown unit is refused, because a mile stored as a kilometre is a
# wrong fact and an iPhone set to miles sends miles. The other metrics have one
# unit in HealthKit (a count, /min, ms) and the client's spelling of it is not
# checked.
_UNIT_FACTORS = {
    'distance': {'km': 1.0, 'm': 0.001, 'mi': 1.609344},
    'walking_speed': {'km/hr': 1.0, 'km/h': 1.0, 'kph': 1.0, 'm/s': 3.6,
                      'mi/hr': 1.609344, 'mph': 1.609344},
    'walking_step_length': {'cm': 1.0, 'm': 100.0, 'in': 2.54},
    'active_energy': {'kcal': 1.0, 'kj': 1 / 4.184},
    'basal_energy': {'kcal': 1.0, 'kj': 1 / 4.184},
    'body_mass': {'kg': 1.0, 'g': 0.001, 'lb': 0.45359237, 'lbs': 0.45359237},
    'sleep_duration': {'h': 1.0, 'hr': 1.0, 'min': 1 / 60},
    'active_minutes': {'min': 1.0, 'h': 60.0, 'hr': 60.0, 's': 1 / 60},
}

# HealthKit holds a percentage as a fraction; clients send either.
_PERCENT_METRICS = frozenset({'spo2', 'walking_double_support_pct'})

# Health Auto Export sleep segments that are time asleep (not Awake, not In Bed).
_ASLEEP_SEGMENTS = frozenset({'asleep', 'core', 'rem', 'deep', 'unspecified'})

_DAY_RE = re.compile(r'\d{4}-\d{2}-\d{2}')


def too_large(content_length) -> bool:
    """True when a declared Content-Length is over the ceiling."""
    try:
        return int(content_length or 0) > MAX_BODY_BYTES
    except (TypeError, ValueError):
        return False


def _day(value, last_day: date) -> date | None:
    """The calendar day a date or timestamp names, as the client wrote it.

    The day is read from the text, not converted to the server's zone: a step
    counted at 23:30 in Denver belongs to that Denver day, as it does in the
    ``export.zip`` parser.
    """
    if not isinstance(value, str) or not _DAY_RE.match(value[:10]):
        return None
    try:
        day = date.fromisoformat(value[:10])
    except ValueError:
        return None
    return day if EARLIEST_DAY <= day <= last_day else None


def _number(value) -> float | None:
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


def _factor(metric_key: str, unit) -> float | None:
    """The factor to the stored unit, or None when the unit cannot be converted."""
    factors = _UNIT_FACTORS.get(metric_key)
    if factors is None:
        return 1.0
    if not isinstance(unit, str):
        return None
    return factors.get(unit.strip().lower())


def _name(value) -> str:
    if not isinstance(value, str) or not value.strip():
        return '(no name)'
    return ''.join(ch for ch in value if ch.isprintable())[:_MAX_NAME_CHARS]


class _Reader:
    """Collects one request's points by metric and day."""

    def __init__(self, today: date):
        # A day ahead of the server's date: the phone may be in a later zone.
        self.last_day = today + timedelta(days=1)
        self.daily: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
        self.unrecognized: set[str] = set()
        self.rejected = 0
        self.points = 0

    def count(self):
        self.points += 1
        if self.points > MAX_POINTS:
            raise AppleHealthPayloadError(
                f'Too many values in one request. The limit is {MAX_POINTS}; send fewer days.',
                status=413)

    def add(self, metric_key: str, day_value, value, unit):
        day = _day(day_value, self.last_day)
        number = _number(value)
        factor = _factor(metric_key, unit)
        if day is None or number is None or factor is None:
            self.rejected += 1
            return
        number *= factor
        if metric_key in _PERCENT_METRICS and number <= 1.0:
            number *= 100.0
        self.daily[metric_key][day].append(number)

    def samples(self) -> list[WearableSample]:
        out = []
        for metric_key in sorted(self.daily):
            for day in sorted(self.daily[metric_key]):
                values = self.daily[metric_key][day]
                total = sum(values)
                value = total if metric_key in CUMULATIVE_METRICS else total / len(values)
                out.append(WearableSample(metric_key=metric_key, date=day, value=round(value, 2)))
        return out


def _read_own(reader: _Reader, samples):
    if not isinstance(samples, list):
        raise AppleHealthPayloadError('"samples" must be a list.')
    for sample in samples:
        reader.count()
        if not isinstance(sample, dict):
            reader.rejected += 1
            continue
        metric_key = _TYPE_TO_METRIC.get(sample.get('type')) if isinstance(
            sample.get('type'), str) else None
        if metric_key is None:
            reader.unrecognized.add(_name(sample.get('type')))
            continue
        reader.add(metric_key, sample.get('date'), sample.get('value'), sample.get('unit'))


def _read_health_auto_export(reader: _Reader, metrics):
    if not isinstance(metrics, list):
        raise AppleHealthPayloadError('"metrics" must be a list.')
    for metric in metrics:
        if not isinstance(metric, dict):
            reader.count()
            reader.rejected += 1
            continue
        name = metric.get('name')
        hk_type = _HAE_NAME_TO_TYPE.get(name.strip().lower()) if isinstance(name, str) else None
        if hk_type is None:
            reader.count()
            reader.unrecognized.add(_name(name))
            continue
        metric_key = _TYPE_TO_METRIC[hk_type]
        units = metric.get('units')
        points = metric.get('data')
        for point in points if isinstance(points, list) else ():
            reader.count()
            if not isinstance(point, dict):
                reader.rejected += 1
                continue
            if metric_key == 'sleep_duration':
                _read_sleep_point(reader, point, units)
            else:
                reader.add(metric_key, point.get('date'), point.get('qty'), units)


def _read_sleep_point(reader: _Reader, point: dict, units):
    """One sleep entry: a night's summary, or one segment of a night."""
    if 'totalSleep' in point or 'asleep' in point:
        # Summarised: one entry per night. totalSleep is the stages added up;
        # an older watch reports only the undivided "asleep"; failing both,
        # the stages themselves.
        stages = [_number(point.get(stage)) or 0.0 for stage in ('core', 'deep', 'rem')]
        hours = next((value for value in (
            _number(point.get('totalSleep')), _number(point.get('asleep')), sum(stages),
        ) if value and value > 0), None)
        # A night with time in bed and no sleep recorded (a phone with no
        # watch) is no sleep data, not a night of none: nothing is stored, as
        # the export parser stores nothing for it.
        if hours is not None:
            reader.add('sleep_duration', point.get('date'), hours, units)
        return
    stage = point.get('value')
    if isinstance(stage, str) and stage.strip().lower() in _ASLEEP_SEGMENTS:
        reader.add('sleep_duration', point.get('startDate') or point.get('date'),
                   point.get('qty'), units)
    # Awake and In Bed are not sleep; they are neither stored nor counted as rejected.


def parse_payload(body, today: date) -> ParsedPayload:
    """Turn a request body into daily samples.

    Raises ``AppleHealthPayloadError`` for a body of neither shape, or one over
    ``MAX_POINTS``.
    """
    if not isinstance(body, dict):
        raise AppleHealthPayloadError(UNREADABLE)
    reader = _Reader(today)
    if 'samples' in body:
        _read_own(reader, body['samples'])
    else:
        data = body.get('data')
        metrics = data.get('metrics') if isinstance(data, dict) else body.get('metrics')
        if metrics is None:
            raise AppleHealthPayloadError(UNREADABLE)
        _read_health_auto_export(reader, metrics)
    return ParsedPayload(
        samples=reader.samples(),
        unrecognized=sorted(reader.unrecognized)[:_MAX_NAMES],
        rejected=reader.rejected,
    )
