"""Parsers for wearable device data files.

Supported formats:
- Garmin FIT files (.fit) — exported from Garmin Connect or USB-mounted watch
- Apple Health export.zip — exported from Apple Health app on iPhone
"""
import io
import logging
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import NamedTuple

logger = logging.getLogger(__name__)


class WearableSample(NamedTuple):
    """A single daily metric extracted from a wearable data file."""
    metric_key: str   # one of WEARABLE_LOINC keys
    date: date
    value: float


# ── Garmin FIT ──────────────────────────────────────────────────────────────

def parse_garmin_fit(file_bytes: bytes) -> list[WearableSample]:
    """Parse a Garmin .fit file and return daily wearable samples.

    Uses the ``fitparse`` library to iterate over FIT messages.  Extracts:
    - steps (from ``monitoring`` or ``session`` messages)
    - resting heart rate, HRV, SpO2, respiratory rate (from ``session``/``record``)
    - active minutes (from ``session`` duration)
    - sleep duration (from ``sleep_level`` messages)
    """
    try:
        from fitparse import FitFile
    except ImportError:
        raise RuntimeError("python-fitparse is required for Garmin FIT uploads: pip install python-fitparse")

    fitfile = FitFile(io.BytesIO(file_bytes))
    fitfile.parse()

    # Accumulators: metric_key → date → list of values (aggregated per day)
    daily: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))

    for record in fitfile.get_messages():
        msg_type = record.name
        fields = {f.name: f.value for f in record.fields}

        ts = fields.get('timestamp')
        if ts is None:
            continue
        if isinstance(ts, datetime):
            d = ts.date()
        else:
            continue

        if msg_type == 'monitoring':
            # Steps from monitoring messages
            steps = fields.get('cycles')
            if steps is not None:
                try:
                    # Garmin stores steps as cycles*2 in some files
                    daily['steps'][d].append(float(steps))
                except (TypeError, ValueError):
                    pass

            hr = fields.get('heart_rate')
            if hr is not None:
                try:
                    daily['resting_hr'][d].append(float(hr))
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'session':
            # Active minutes from session duration (total_timer_time in seconds)
            timer_time = fields.get('total_timer_time')
            if timer_time is not None:
                try:
                    daily['active_minutes'][d].append(float(timer_time) / 60.0)
                except (TypeError, ValueError):
                    pass

            # Steps from session if available
            total_steps = fields.get('total_steps') or fields.get('total_cycles')
            if total_steps is not None:
                try:
                    daily['steps'][d].append(float(total_steps))
                except (TypeError, ValueError):
                    pass

            # SpO2
            spo2 = fields.get('saturated_hemoglobin_percent') or fields.get('avg_saturated_hemoglobin_percent')
            if spo2 is not None:
                try:
                    val = float(spo2)
                    if val <= 1.0:
                        val *= 100.0
                    daily['spo2'][d].append(val)
                except (TypeError, ValueError):
                    pass

            # Respiratory rate
            rr = fields.get('avg_respiration_rate') or fields.get('respiration_rate')
            if rr is not None:
                try:
                    daily['respiratory_rate'][d].append(float(rr))
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'record':
            hr = fields.get('heart_rate')
            if hr is not None:
                try:
                    daily['resting_hr'][d].append(float(hr))
                except (TypeError, ValueError):
                    pass

        elif msg_type in ('sleep_level', 'sleep_data'):
            # Sleep duration: compute from timestamp span if we see start/end
            duration_sec = fields.get('total_timer_time')
            if duration_sec is not None:
                try:
                    daily['sleep_duration'][d].append(float(duration_sec) / 3600.0)
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'stress':
            # HRV SDNN sometimes in stress messages
            hrv = fields.get('heart_rate_variability')
            if hrv is not None:
                try:
                    daily['hrv_sdnn'][d].append(float(hrv))
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'hrv':
            # Dedicated HRV message type
            sdnn = fields.get('weekly_average') or fields.get('sdnn')
            if sdnn is not None:
                try:
                    daily['hrv_sdnn'][d].append(float(sdnn))
                except (TypeError, ValueError):
                    pass

    # Collapse daily lists to single values
    samples: list[WearableSample] = []
    for metric_key, date_map in daily.items():
        for d, values in date_map.items():
            if metric_key == 'steps':
                # Sum steps across the day
                agg = sum(values)
            elif metric_key in ('active_minutes', 'sleep_duration'):
                # Sum durations
                agg = sum(values)
            else:
                # Average for rates/percentages
                agg = sum(values) / len(values)
            samples.append(WearableSample(metric_key=metric_key, date=d, value=round(agg, 2)))

    logger.info('garmin_fit_parsed samples=%d metrics=%s', len(samples), sorted(daily.keys()))
    return samples


# ── Apple Health ────────────────────────────────────────────────────────────

# HKQuantityType → our metric key
_APPLE_TYPE_MAP = {
    'HKQuantityTypeIdentifierStepCount': 'steps',
    'HKQuantityTypeIdentifierAppleExerciseTime': 'active_minutes',
    'HKQuantityTypeIdentifierRestingHeartRate': 'resting_hr',
    'HKQuantityTypeIdentifierHeartRateVariabilitySDNN': 'hrv_sdnn',
    'HKQuantityTypeIdentifierOxygenSaturation': 'spo2',
    'HKQuantityTypeIdentifierRespiratoryRate': 'respiratory_rate',
}

_APPLE_SLEEP_TYPE = 'HKCategoryTypeIdentifierSleepAnalysis'


def parse_apple_health_export(zip_bytes: bytes) -> list[WearableSample]:
    """Parse an Apple Health export.zip and return daily wearable samples.

    Streams ``apple_health_export/export.xml`` using ``iterparse`` to keep
    memory usage low on large exports.
    """
    import xml.etree.ElementTree as ET

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("Uploaded file is not a valid ZIP archive.")

    # Find the export.xml inside the zip
    xml_name = None
    for name in zf.namelist():
        if name.endswith('export.xml'):
            xml_name = name
            break
    if xml_name is None:
        raise ValueError("ZIP does not contain export.xml. Please upload the Apple Health export.zip.")

    daily: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))

    with zf.open(xml_name) as xml_file:
        for event, elem in ET.iterparse(xml_file, events=('end',)):
            if elem.tag != 'Record':
                elem.clear()
                continue

            record_type = elem.get('type', '')
            metric_key = _APPLE_TYPE_MAP.get(record_type)

            if metric_key is not None:
                # Quantity record
                value_str = elem.get('value')
                start_str = elem.get('startDate', '')
                if value_str and start_str:
                    try:
                        val = float(value_str)
                        # SpO2 is stored as fraction (0-1) in Apple Health
                        if metric_key == 'spo2' and val <= 1.0:
                            val *= 100.0
                        d = _parse_apple_date(start_str)
                        if d is not None:
                            daily[metric_key][d].append(val)
                    except (TypeError, ValueError):
                        pass

            elif record_type == _APPLE_SLEEP_TYPE:
                # Sleep: compute duration from start/end
                start_str = elem.get('startDate', '')
                end_str = elem.get('endDate', '')
                # Only count "asleep" categories (skip InBed)
                sleep_value = elem.get('value', '')
                if 'Asleep' in sleep_value or 'asleep' in sleep_value.lower() if sleep_value else False:
                    if start_str and end_str:
                        try:
                            start_dt = _parse_apple_datetime(start_str)
                            end_dt = _parse_apple_datetime(end_str)
                            if start_dt and end_dt and end_dt > start_dt:
                                hours = (end_dt - start_dt).total_seconds() / 3600.0
                                d = start_dt.date()
                                daily['sleep_duration'][d].append(hours)
                        except (TypeError, ValueError):
                            pass

            elem.clear()

    # Collapse daily lists to single values
    samples: list[WearableSample] = []
    for metric_key, date_map in daily.items():
        for d, values in date_map.items():
            if metric_key in ('steps', 'active_minutes', 'sleep_duration'):
                agg = sum(values)
            else:
                agg = sum(values) / len(values)
            samples.append(WearableSample(metric_key=metric_key, date=d, value=round(agg, 2)))

    logger.info('apple_health_parsed samples=%d metrics=%s', len(samples), sorted(daily.keys()))
    return samples


def _parse_apple_date(s: str) -> date | None:
    """Parse Apple Health date string like '2024-01-15 08:30:00 -0700' to date."""
    try:
        # Format: "2024-01-15 08:30:00 -0700"
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except (ValueError, IndexError):
        return None


def _parse_apple_datetime(s: str) -> datetime | None:
    """Parse Apple Health datetime string to datetime."""
    try:
        # "2024-01-15 08:30:00 -0700" — strip timezone for simplicity
        return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, IndexError):
        return None
