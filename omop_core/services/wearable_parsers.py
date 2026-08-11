"""Parsers for wearable device data files.

Supported formats:
- Garmin FIT files (.fit) — exported from Garmin Connect or USB-mounted watch
- Apple Health export.zip — exported from Apple Health app on iPhone
"""
import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import NamedTuple

logger = logging.getLogger(__name__)


class WearableSample(NamedTuple):
    """A single daily metric extracted from a wearable data file."""
    metric_key: str   # one of WEARABLE_CONCEPT_CODE keys
    date: date
    value: float


# ── Garmin FIT ──────────────────────────────────────────────────────────────

def parse_garmin_fit(file_bytes: bytes) -> list[WearableSample]:
    """Parse a Garmin .fit file and return daily wearable samples.

    Uses the ``fitparse`` library to iterate over FIT messages.  Extracts:
    - steps (from ``monitoring`` or ``session`` messages)
    - resting heart rate (from ``monitoring_hr_data`` or p10 of monitoring HR)
    - HRV RMSSD (from ``hrv_status_summary``, ``hrv_value``, or legacy ``hrv``);
      Garmin's HRV Status is RMSSD-based, so it is NOT stored as SDNN (#438)
    - SpO2 (from ``spo2_data`` or ``session``)
    - respiratory rate (from ``respiration_rate`` messages or ``session``)
    - active minutes (from ``monitoring`` active_time or ``session`` duration)
    - sleep duration (from ``sleep_level`` timestamp spans)
    - VO2 Max (from ``session`` enhanced_max_oxygen_consumption)
    """
    try:
        from fitparse import FitFile
    except ImportError:
        raise RuntimeError("python-fitparse is required for Garmin FIT uploads: pip install python-fitparse")

    fitfile = FitFile(io.BytesIO(file_bytes))
    fitfile.parse()

    # Accumulators: metric_key -> date -> list of values (aggregated per day)
    daily: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))

    # Separate step sources to avoid double-counting:
    # monitoring_steps has incremental cycle counts; session_steps has activity totals.
    # We prefer monitoring (full-day) over session (activity subset) when both exist.
    monitoring_steps: dict[date, list[float]] = defaultdict(list)
    session_steps: dict[date, list[float]] = defaultdict(list)

    # Separate HR sources: monitoring HR includes active periods and overestimates
    # resting HR. We collect monitoring HR separately and use the 10th percentile
    # as a resting HR proxy (closest to true resting without dedicated resting HR data).
    monitoring_hr: dict[date, list[float]] = defaultdict(list)

    # Sleep level entries: collect (timestamp, level) to compute duration in post-processing
    sleep_entries: list[tuple[datetime, int]] = []

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
            # Steps: newer devices (MARQ 2, Fenix 7+) use 'steps' directly;
            # older devices use 'cycles' as an incremental step counter.
            _steps = fields.get('steps')
            steps = _steps if _steps is not None else fields.get('cycles')
            if steps is not None:
                try:
                    monitoring_steps[d].append(float(steps))
                except (TypeError, ValueError):
                    pass

            # Active time from monitoring messages (seconds)
            active_time = fields.get('active_time')
            if active_time is not None:
                try:
                    daily['active_minutes'][d].append(float(active_time) / 60.0)
                except (TypeError, ValueError):
                    pass

            # Distance from monitoring (meters → km)
            dist = fields.get('distance')
            if dist is not None:
                try:
                    daily['distance'][d].append(float(dist) / 1000.0)
                except (TypeError, ValueError):
                    pass

            # Active calories from monitoring
            acal = fields.get('active_calories')
            if acal is not None:
                try:
                    val = float(acal)
                    if val > 0:
                        daily['active_energy'][d].append(val)
                except (TypeError, ValueError):
                    pass

            # All-day HR samples -- NOT resting HR; collected separately
            hr = fields.get('heart_rate')
            if hr is not None:
                try:
                    monitoring_hr[d].append(float(hr))
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'monitoring_info':
            # Resting metabolic rate (kcal/day) — basal energy
            rmr = fields.get('resting_metabolic_rate')
            if rmr is not None:
                try:
                    val = float(rmr)
                    if val > 0:
                        daily['basal_energy'][d].append(val)
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'monitoring_hr_data':
            # Dedicated resting HR message (MARQ 2, Fenix 7+, Venu, etc.)
            rhr = fields.get('resting_heart_rate')
            if rhr is not None:
                try:
                    daily['resting_hr'][d].append(float(rhr))
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

            # Session step totals (activity subset, used as fallback only)
            total_steps = fields.get('total_steps') or fields.get('total_cycles')
            if total_steps is not None:
                try:
                    session_steps[d].append(float(total_steps))
                except (TypeError, ValueError):
                    pass

            # Session distance (meters → km)
            total_dist = fields.get('total_distance')
            if total_dist is not None:
                try:
                    daily['distance'][d].append(float(total_dist) / 1000.0)
                except (TypeError, ValueError):
                    pass

            # Session calories (active energy from exercise)
            total_cal = fields.get('total_calories')
            if total_cal is not None:
                try:
                    val = float(total_cal)
                    if val > 0:
                        daily['active_energy'][d].append(val)
                except (TypeError, ValueError):
                    pass

            # SpO2 from activity sessions (fallback; spo2_data is preferred)
            spo2 = fields.get('saturated_hemoglobin_percent') or fields.get('avg_saturated_hemoglobin_percent')
            if spo2 is not None:
                try:
                    val = float(spo2)
                    if val <= 1.0:
                        val *= 100.0
                    if 50 < val <= 100:
                        daily['spo2'][d].append(val)
                except (TypeError, ValueError):
                    pass

            # Respiratory rate from activity sessions (fallback; respiration_rate msg preferred)
            rr = fields.get('avg_respiration_rate') or fields.get('respiration_rate')
            if rr is not None:
                try:
                    val = float(rr)
                    if val > 0:
                        daily['respiratory_rate'][d].append(val)
                except (TypeError, ValueError):
                    pass

            # VO2 Max from activity sessions
            vo2 = fields.get('enhanced_max_oxygen_consumption') or fields.get('vo2_max')
            if vo2 is not None:
                try:
                    val = float(vo2)
                    if 10 <= val <= 100:
                        daily['vo2_max'][d].append(val)
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'respiration_rate':
            # Top-level respiration rate messages (monitoring files)
            rr = fields.get('respiration_rate')
            if rr is not None:
                try:
                    val = float(rr)
                    if val > 0:  # -1 or -2 means no valid reading
                        daily['respiratory_rate'][d].append(val)
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'spo2_data':
            # Pulse ox readings (monitoring files, when pulse ox is enabled)
            spo2 = fields.get('reading_spo2')
            if spo2 is not None:
                try:
                    val = float(spo2)
                    if 50 < val <= 100:  # filter invalid readings (0, 255, etc.)
                        daily['spo2'][d].append(val)
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'hrv_status_summary':
            # Daily/weekly HRV summary (newer devices).
            #
            # This is RMSSD, not SDNN. Garmin's HRV Status is computed as the
            # root mean square of successive differences over overnight
            # readings, and weekly_average is the 7-day rolling mean it
            # displays. It was previously stored as hrv_sdnn, which filed it
            # under LOINC 80404-7 — a code that specifically means the
            # standard-deviation form. See #438.
            rmssd = fields.get('weekly_average') or fields.get('last_night_average')
            if rmssd is not None:
                try:
                    val = float(rmssd)
                    if val > 0:
                        daily['hrv_rmssd'][d].append(val)
                except (TypeError, ValueError):
                    pass

        elif msg_type == 'hrv_value':
            # Individual 5-minute HRV readings (ms) from the same HRV Status
            # feature as hrv_status_summary above, so likewise RMSSD.
            hrv_val = fields.get('value')
            if hrv_val is not None:
                try:
                    val = float(hrv_val)
                    if val > 0:
                        daily['hrv_rmssd'][d].append(val)
                except (TypeError, ValueError):
                    pass

        elif msg_type in ('sleep_level', 'sleep_data'):
            # Sleep level entries: collect timestamp + level for duration computation.
            # level 0 = awake/unmeasured, 1-3 = light/deep/REM sleep stages.
            # Also check total_timer_time as fallback (older format).
            duration_sec = fields.get('total_timer_time')
            if duration_sec is not None:
                try:
                    daily['sleep_duration'][d].append(float(duration_sec) / 3600.0)
                except (TypeError, ValueError):
                    pass
            else:
                # Newer format: individual sleep_level entries with timestamps
                level = fields.get('sleep_level')
                if level is not None:
                    try:
                        sleep_entries.append((ts, int(level)))
                    except (TypeError, ValueError):
                        pass

        elif msg_type == 'stress':
            # Stress messages contain stress_level_value, NOT usable HRV.
            # Skip -- HRV comes from hrv_status_summary/hrv_value instead.
            pass

        elif msg_type == 'hrv':
            # Legacy HRV message type (fallback for older devices).
            #
            # This message can carry either statistic, so each field is routed
            # to the metric it actually is rather than to whichever one is
            # checked first: 'weekly_average' is the HRV Status rolling mean
            # (RMSSD), while a field named 'sdnn' is the standard-deviation
            # form. Previously both were stored as hrv_sdnn (#438).
            for field_name, metric_key in (('weekly_average', 'hrv_rmssd'),
                                           ('sdnn', 'hrv_sdnn')):
                raw = fields.get(field_name)
                if raw is None:
                    continue
                try:
                    val = float(raw)
                except (TypeError, ValueError):
                    continue
                if val > 0:
                    daily[metric_key][d].append(val)

    # Compute sleep duration from sleep_level timestamp spans.
    # Consecutive entries with level > 0 (not awake) contribute to sleep duration.
    if sleep_entries:
        sleep_entries.sort(key=lambda x: x[0])
        for i in range(len(sleep_entries) - 1):
            ts_curr, level_curr = sleep_entries[i]
            ts_next = sleep_entries[i + 1][0]
            if level_curr > 0:  # 1=light, 2=deep, 3=REM
                span_hours = (ts_next - ts_curr).total_seconds() / 3600.0
                if 0 < span_hours < 4:  # cap individual span at 4h to filter gaps
                    d = ts_curr.date()
                    daily['sleep_duration'][d].append(span_hours)

    # Merge step sources: prefer monitoring (full-day) over session (activity subset).
    # monitoring.cycles is a cumulative counter, so take the max (= total) not the sum.
    all_step_dates = set(monitoring_steps.keys()) | set(session_steps.keys())
    for d in all_step_dates:
        if monitoring_steps.get(d):
            daily['steps'][d] = [max(monitoring_steps[d])]
        elif session_steps.get(d):
            daily['steps'][d] = session_steps[d]

    # Approximate resting HR from monitoring data using the 10th percentile.
    # All-day monitoring includes active HR which inflates the average. The
    # 10th percentile captures the lower range (rest/sleep) without being
    # as noisy as the absolute minimum.
    # Skip p10 estimation for dates that already have a dedicated resting HR
    # (from monitoring_hr_data messages on newer devices like MARQ 2, Fenix 7+).
    dates_with_dedicated_rhr = {d for d, vs in daily['resting_hr'].items() if vs}
    for d, hr_values in monitoring_hr.items():
        if hr_values and d not in dates_with_dedicated_rhr:
            sorted_hr = sorted(hr_values)
            p10_idx = max(0, len(sorted_hr) // 10)
            resting_estimate = sorted_hr[p10_idx]
            daily['resting_hr'][d].append(resting_estimate)

    # Collapse daily lists to single values
    samples: list[WearableSample] = []
    for metric_key, date_map in daily.items():
        for d, values in date_map.items():
            if metric_key in ('steps', 'active_minutes', 'sleep_duration',
                              'distance', 'flights_climbed', 'active_energy', 'basal_energy'):
                agg = sum(values)
            else:
                # Average for rates/percentages
                agg = sum(values) / len(values)
            samples.append(WearableSample(metric_key=metric_key, date=d, value=round(agg, 2)))

    logger.info('garmin_fit_parsed samples=%d metrics=%s', len(samples), sorted(daily.keys()))
    return samples


# ── Apple Health ────────────────────────────────────────────────────────────

# HKQuantityType -> our metric key
_APPLE_TYPE_MAP = {
    'HKQuantityTypeIdentifierStepCount': 'steps',
    'HKQuantityTypeIdentifierAppleExerciseTime': 'active_minutes',
    'HKQuantityTypeIdentifierRestingHeartRate': 'resting_hr',
    'HKQuantityTypeIdentifierHeartRateVariabilitySDNN': 'hrv_sdnn',
    'HKQuantityTypeIdentifierOxygenSaturation': 'spo2',
    'HKQuantityTypeIdentifierRespiratoryRate': 'respiratory_rate',
    'HKQuantityTypeIdentifierVO2Max': 'vo2_max',
    'HKQuantityTypeIdentifierDistanceWalkingRunning': 'distance',
    'HKQuantityTypeIdentifierWalkingSpeed': 'walking_speed',
    'HKQuantityTypeIdentifierWalkingStepLength': 'walking_step_length',
    'HKQuantityTypeIdentifierWalkingDoubleSupportPercentage': 'walking_double_support_pct',
    'HKQuantityTypeIdentifierWalkingHeartRateAverage': 'walking_hr_avg',
    'HKQuantityTypeIdentifierFlightsClimbed': 'flights_climbed',
    'HKQuantityTypeIdentifierActiveEnergyBurned': 'active_energy',
    'HKQuantityTypeIdentifierBasalEnergyBurned': 'basal_energy',
    'HKQuantityTypeIdentifierBodyMass': 'body_mass',
}

_APPLE_SLEEP_TYPE = 'HKCategoryTypeIdentifierSleepAnalysis'
# General HeartRate can be used to derive resting HR when RestingHeartRate isn't available
_APPLE_GENERAL_HR_TYPE = 'HKQuantityTypeIdentifierHeartRate'


_MAX_XML_UNCOMPRESSED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB zip bomb limit


def parse_apple_health_export(zip_bytes: bytes) -> list[WearableSample]:
    """Parse an Apple Health export.zip and return daily wearable samples.

    Streams ``apple_health_export/export.xml`` using ``iterparse`` to keep
    memory usage low on large exports.  Uses stdlib ``xml.etree.ElementTree``
    (defusedxml does not expose ``iterparse``).  The root element is cleared
    after each record to prevent memory accumulation.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise ValueError("Uploaded file is not a valid ZIP archive.")

    # Find the export.xml inside the zip
    xml_name = None
    for info in zf.infolist():
        if info.filename.endswith('export.xml'):
            # Zip bomb protection: reject implausibly large uncompressed XML
            if info.file_size > _MAX_XML_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"export.xml uncompressed size ({info.file_size / (1024**3):.1f} GB) "
                    f"exceeds the {_MAX_XML_UNCOMPRESSED_BYTES // (1024**3)} GB limit."
                )
            xml_name = info.filename
            break
    if xml_name is None:
        raise ValueError("ZIP does not contain export.xml. Please upload the Apple Health export.zip.")

    daily: dict[str, dict[date, list[float]]] = defaultdict(lambda: defaultdict(list))
    # Collect general HeartRate readings separately; used to derive resting HR
    # when the device doesn't export RestingHeartRate directly.
    general_hr: dict[date, list[float]] = defaultdict(list)

    with zf.open(xml_name) as xml_file:
        context = ET.iterparse(xml_file, events=('start', 'end'))
        root = None
        for event, elem in context:
            if root is None:
                root = elem
            if event != 'end':
                continue
            if elem.tag != 'Record':
                elem.clear()
                continue

            record_type = elem.get('type', '')
            metric_key = _APPLE_TYPE_MAP.get(record_type)

            if record_type == _APPLE_GENERAL_HR_TYPE:
                # Collect general HR for resting-HR derivation
                value_str = elem.get('value')
                start_str = elem.get('startDate', '')
                if value_str and start_str:
                    try:
                        val = float(value_str)
                        d = _parse_apple_date(start_str)
                        if d is not None and 20 < val < 250:
                            general_hr[d].append(val)
                    except (TypeError, ValueError):
                        pass
                elem.clear()
                if root is not None:
                    root.clear()
                continue

            elif metric_key is not None:
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
                if sleep_value and 'asleep' in sleep_value.lower():
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
            if root is not None:
                root.clear()

    # Derive resting HR from general HeartRate if no explicit RestingHeartRate data
    if general_hr and not daily.get('resting_hr'):
        for d, hr_values in general_hr.items():
            if len(hr_values) >= 5:
                # 10th percentile of daily readings approximates resting HR
                sorted_hr = sorted(hr_values)
                idx = max(0, int(len(sorted_hr) * 0.10))
                resting_estimate = sorted_hr[idx]
                daily['resting_hr'][d].append(resting_estimate)
        if daily.get('resting_hr'):
            logger.info('apple_health_derived_resting_hr days=%d from general HeartRate', len(daily['resting_hr']))

    # Collapse daily lists to single values
    samples: list[WearableSample] = []
    for metric_key, date_map in daily.items():
        for d, values in date_map.items():
            if metric_key in ('steps', 'active_minutes', 'sleep_duration',
                              'distance', 'flights_climbed', 'active_energy', 'basal_energy'):
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
        # "2024-01-15 08:30:00 -0700" -- strip timezone for simplicity
        return datetime.strptime(s[:19], '%Y-%m-%d %H:%M:%S')
    except (ValueError, IndexError):
        return None
