"""Parser for an Oura Ring data export (#2889).

Oura's Membership Hub exports a member's data as a zip of CSV files that
follow the Oura API V2 data models, durations in seconds. The file names and
the columns read here are the API V2 ones. They have **not been checked
against a real export**, so a file or column that is not there is skipped,
never guessed at.

What is read:

* ``dailyactivity``: steps, active minutes (high and medium activity time) and
  active energy (``active_calories``);
* ``sleepmodel``: sleep duration (the day's sessions added) and, from the
  day's longest session, average HRV (RMSSD, stored as such and never as
  SDNN), lowest heart rate (the resting heart rate Oura reports) and average
  breathing rate;
* ``dailyspo2``: average SpO2; ``vo2max``: VO2 max.

Not read: Oura's scores (sleep, readiness), temperature, stress and the
heart-rate series. Its "equivalent walking distance" is an estimate from
activity, not a distance walked, so it is not stored as ``distance``.
"""
import csv
import io
import logging
import re
import zipfile
from collections import defaultdict
from datetime import date, datetime

from omop_core.services.wearable_parsers import CUMULATIVE_METRICS, WearableSample

logger = logging.getLogger(__name__)

MAX_MEMBER_BYTES = 200 * 1024 * 1024    # one CSV, uncompressed
MAX_TOTAL_BYTES = 500 * 1024 * 1024     # every CSV of one upload
MAX_MEMBERS = 200
# Sleep sessions Oura marks as removed or as rest are not sleep.
NOT_SLEEP = frozenset({'deleted', 'rest'})

_KINDS = {
    'dailyactivity': 'activity', 'sleepmodel': 'sleep', 'sleep': 'sleep',
    'dailyspo2': 'spo2', 'vo2max': 'vo2',
}


def file_kind(filename: str) -> str | None:
    """Which Oura document a file holds, from its name: ``dailyactivity.csv``,
    ``daily_activity.csv`` and ``DailyActivity.csv`` are one kind."""
    base = filename.replace('\\', '/').rsplit('/', 1)[-1].lower()
    if not base.endswith('.csv') or base.startswith('.'):
        return None
    return _KINDS.get(''.join(ch for ch in base[:-4] if ch.isalnum()))


def _rows(text: str):
    """The rows of one CSV as dicts keyed by lower-cased header. Oura's export
    separates with ``;``; a comma file is read as well."""
    text = text.lstrip('﻿')
    first = text.split('\n', 1)[0]
    delimiter = ';' if first.count(';') > first.count(',') else ','
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        yield {(key or '').strip().lower(): value.strip()
               for key, value in row.items() if isinstance(value, str)}


def _number(raw) -> float | None:
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float('inf') else None


def _day(raw) -> date | None:
    try:
        return datetime.strptime((raw or '')[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _spo2(raw) -> float | None:
    """A day's average SpO2: a plain number, or the ``average`` of the object
    API V2 nests it in (``spo2_percentage``)."""
    number = _number(raw)
    if number is not None:
        return number
    match = re.search(r'average["\']?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)', raw or '')
    return float(match.group(1)) if match else None


def _collect(kind: str, rows, daily, sleep_sessions) -> None:
    for row in rows:
        day = _day(row.get('day'))
        if day is None:
            continue
        if kind == 'activity':
            steps = _number(row.get('steps'))
            if steps is not None:
                daily['steps'][day].append(steps)
            seconds = [_number(row.get(name)) for name in ('high_activity_time', 'medium_activity_time')]
            if any(value is not None for value in seconds):
                daily['active_minutes'][day].append(sum(v for v in seconds if v) / 60.0)
            calories = _number(row.get('active_calories'))
            if calories is not None:
                daily['active_energy'][day].append(calories)
        elif kind == 'sleep':
            if row.get('type', '').lower() in NOT_SLEEP:
                continue
            sleep_sessions[day].append({
                'seconds': _number(row.get('total_sleep_duration')),
                'hrv': _number(row.get('average_hrv')),
                'heart_rate': _number(row.get('lowest_heart_rate')),
                'breath': _number(row.get('average_breath')),
            })
        elif kind == 'spo2':
            value = _spo2(row.get('spo2_percentage') or row.get('spo2_percentage.average'))
            if value is not None:
                daily['spo2'][day].append(value)
        elif kind == 'vo2':
            value = _number(row.get('vo2_max'))
            if value is not None:
                daily['vo2_max'][day].append(value)


def _members(file_bytes: bytes, filename: str) -> list[tuple[str, bytes]]:
    """The CSV files of the upload: the file itself, or those in the zip."""
    if filename.lower().endswith('.csv'):
        return [(filename, file_bytes)]
    try:
        archive = zipfile.ZipFile(io.BytesIO(file_bytes))
    except zipfile.BadZipFile:
        raise ValueError('Uploaded file is not a valid ZIP archive.')
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(infos) > MAX_MEMBERS:
        raise ValueError('The archive holds too many files.')
    wanted = [info for info in infos if file_kind(info.filename)]
    # Zip bomb protection, on what the archive says and on what is read.
    if sum(info.file_size for info in wanted) > MAX_TOTAL_BYTES:
        raise ValueError('The archive is too large once unpacked.')
    members = []
    for info in wanted:
        if info.file_size > MAX_MEMBER_BYTES:
            raise ValueError(f'{info.filename} is too large once unpacked.')
        with archive.open(info) as handle:
            data = handle.read(MAX_MEMBER_BYTES + 1)
        if len(data) > MAX_MEMBER_BYTES:
            raise ValueError(f'{info.filename} is too large once unpacked.')
        members.append((info.filename, data))
    return members


def parse_oura_export(file_bytes: bytes, filename: str = '') -> list[WearableSample]:
    """Daily samples from an Oura export: the zip, or one of its CSV files.

    Raises ``ValueError`` for a zip that is not one, or holds none of the
    files read.
    """
    daily: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    sleep_sessions: dict[date, list[dict]] = defaultdict(list)
    found = 0
    for name, data in _members(file_bytes, filename):
        kind = file_kind(name)
        if kind is None:
            continue
        found += 1
        _collect(kind, _rows(data.decode('utf-8', errors='replace')), daily, sleep_sessions)
    if not found:
        raise ValueError('No Oura export files found (dailyactivity, sleepmodel, dailyspo2 or vo2max).')

    for day, sessions in sleep_sessions.items():
        timed = [s for s in sessions if s['seconds'] and s['seconds'] > 0]
        if not timed:
            continue
        daily['sleep_duration'][day].append(sum(s['seconds'] for s in timed) / 3600.0)
        main = max(timed, key=lambda s: s['seconds'])
        # Oura writes 0 where it has no figure.
        for metric, key in (('hrv_rmssd', 'hrv'), ('resting_hr', 'heart_rate'),
                            ('respiratory_rate', 'breath')):
            if main[key]:
                daily[metric][day].append(main[key])

    samples = [
        WearableSample(metric_key=metric, date=d,
                       value=round(sum(values) if metric in CUMULATIVE_METRICS
                                   else sum(values) / len(values), 2))
        for metric, by_day in daily.items() for d, values in by_day.items()
    ]
    logger.info('oura_parsed samples=%d metrics=%s', len(samples), sorted(daily))
    return samples
