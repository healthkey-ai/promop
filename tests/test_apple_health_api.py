"""The Apple Health sync endpoint's body reader (#2800).

No database: `parse_payload` turns a request body into the daily samples the
`export.zip` parser produces. The endpoint, its token and the replace rule are
covered by `patient_portal.tests.AppleHealthSyncEndpointTest`.
"""
from datetime import date, timedelta

import pytest

from omop_core.services import apple_health_api as api
from omop_core.services.mappings import WEARABLE_ARTIFACT_BOUNDS, WEARABLE_CONCEPT_CODE
from omop_core.services.wearable_parsers import (
    CUMULATIVE_METRICS, WearableSample, _APPLE_SLEEP_TYPE, _APPLE_TYPE_MAP,
)

TODAY = date(2026, 11, 23)
DAY = date(2026, 11, 20)
STEPS = 'HKQuantityTypeIdentifierStepCount'


def own(*samples):
    return api.parse_payload({'samples': list(samples)}, TODAY)


def s(hk_type, value, day=DAY, **extra):
    return {'type': hk_type, 'date': day.isoformat() if isinstance(day, date) else day,
            'value': value, **extra}


def hae(*metrics, wrapped=True):
    body = {'metrics': list(metrics)}
    return api.parse_payload({'data': body} if wrapped else body, TODAY)


def metric(name, units, *points):
    return {'name': name, 'units': units, 'data': list(points)}


def values(parsed):
    return {(sample.metric_key, sample.date): sample.value for sample in parsed.samples}


# -- promop's own shape -------------------------------------------------------

def test_every_mapped_healthkit_type_is_read_under_its_metric_key():
    types = {**_APPLE_TYPE_MAP, _APPLE_SLEEP_TYPE: 'sleep_duration'}
    stored_unit = {metric_key: next(unit for unit, factor in factors.items() if factor == 1.0)
                   for metric_key, factors in api._UNIT_FACTORS.items()}
    parsed = own(*[s(hk_type, 50, **({'unit': stored_unit[metric_key]}
                                     if metric_key in stored_unit else {}))
                   for hk_type, metric_key in types.items()])
    assert parsed.unrecognized == [] and parsed.rejected == 0
    assert {sample.metric_key for sample in parsed.samples} == set(types.values())
    assert all(isinstance(sample, WearableSample) for sample in parsed.samples)


def test_a_days_readings_are_added_up_or_averaged_as_the_export_parser_does():
    parsed = own(s(STEPS, 4000), s(STEPS, 5200.4),
                 s('HKQuantityTypeIdentifierRestingHeartRate', 50),
                 s('HKQuantityTypeIdentifierRestingHeartRate', 55),
                 s(STEPS, 100, day=DAY - timedelta(days=1)))
    assert values(parsed) == {
        ('steps', DAY): 9200.4,
        ('resting_hr', DAY): 52.5,
        ('steps', DAY - timedelta(days=1)): 100.0,
    }
    assert 'steps' in CUMULATIVE_METRICS and 'resting_hr' not in CUMULATIVE_METRICS


def test_samples_come_out_in_a_stable_order():
    parsed = own(s(STEPS, 1, day=DAY), s('HKQuantityTypeIdentifierBodyMass', 70, unit='kg'),
                 s(STEPS, 1, day=DAY - timedelta(days=2)))
    assert [(x.metric_key, x.date) for x in parsed.samples] == [
        ('body_mass', DAY), ('steps', DAY - timedelta(days=2)), ('steps', DAY)]


@pytest.mark.parametrize('hk_type, value, unit, stored', [
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 5, 'mi', 8.05),
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 6400, 'm', 6.4),
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 6.4, 'km', 6.4),
    ('HKQuantityTypeIdentifierWalkingSpeed', 1.2, 'm/s', 4.32),
    ('HKQuantityTypeIdentifierWalkingSpeed', 3, 'MPH', 4.83),
    ('HKQuantityTypeIdentifierWalkingStepLength', 0.7, 'm', 70.0),
    ('HKQuantityTypeIdentifierWalkingStepLength', 28, 'in', 71.12),
    ('HKQuantityTypeIdentifierActiveEnergyBurned', 2092, 'kJ', 500.0),
    ('HKQuantityTypeIdentifierBasalEnergyBurned', 1600, 'kcal', 1600.0),
    ('HKQuantityTypeIdentifierBodyMass', 154, 'lb', 69.85),
    (_APPLE_SLEEP_TYPE, 450, 'min', 7.5),
    ('HKQuantityTypeIdentifierAppleExerciseTime', 1.5, ' hr ', 90.0),
])
def test_a_known_unit_is_converted_to_the_unit_the_row_is_stored_in(hk_type, value, unit, stored):
    parsed = own(s(hk_type, value, unit=unit))
    assert [x.value for x in parsed.samples] == [stored]


@pytest.mark.parametrize('hk_type, unit', [
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 'yd'),
    ('HKQuantityTypeIdentifierBodyMass', 'stone'),
    ('HKQuantityTypeIdentifierActiveEnergyBurned', 'cal'),
    ('HKQuantityTypeIdentifierWalkingSpeed', 7),
    # No unit at all: an iPhone set to miles sends miles, so none is assumed.
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', None),
    ('HKQuantityTypeIdentifierBodyMass', ''),
    (_APPLE_SLEEP_TYPE, None),
])
def test_an_unknown_or_missing_unit_is_refused_not_guessed(hk_type, unit):
    parsed = own(s(hk_type, 10, unit=unit))
    assert parsed.samples == [] and parsed.rejected == 1


def test_a_single_unit_metrics_unit_is_not_checked():
    parsed = own(s(STEPS, 9200, unit='steps'),
                 s('HKQuantityTypeIdentifierVO2Max', 41.5, unit='ml/(kg·min)'))
    assert values(parsed) == {('steps', DAY): 9200.0, ('vo2_max', DAY): 41.5}


def test_a_percentage_sent_as_a_fraction_is_a_percentage():
    parsed = own(s('HKQuantityTypeIdentifierOxygenSaturation', 0.97),
                 s('HKQuantityTypeIdentifierWalkingDoubleSupportPercentage', 28.5))
    assert values(parsed) == {('spo2', DAY): 97.0, ('walking_double_support_pct', DAY): 28.5}


@pytest.mark.parametrize('day', [
    '2026-11-20', '2026-11-20 23:30:00 -0700', '2026-11-20T23:30:00-07:00',
])
def test_the_day_is_the_one_the_phone_wrote(day):
    assert values(own(s(STEPS, 10, day=day))) == {('steps', DAY): 10.0}


@pytest.mark.parametrize('day', [
    None, '', 'yesterday', '20/11/2026', '2026-13-01', '2026-02-30', 20261120,
    '2013-12-31',                                  # before HealthKit existed
    (TODAY + timedelta(days=2)).isoformat(),       # not happened anywhere yet
])
def test_a_day_that_cannot_be_right_is_rejected(day):
    parsed = own(s(STEPS, 10, day=day))
    assert parsed.samples == [] and parsed.rejected == 1


def test_tomorrow_by_the_servers_clock_is_today_somewhere():
    tomorrow = TODAY + timedelta(days=1)
    assert values(own(s(STEPS, 10, day=tomorrow))) == {('steps', tomorrow): 10.0}


@pytest.mark.parametrize('value', [
    None, '', 'many', True, False, [1], {'qty': 1}, float('nan'), float('inf'), '1' * 40,
])
def test_a_value_that_is_not_a_finite_number_is_rejected(value):
    parsed = own(s(STEPS, value))
    assert parsed.samples == [] and parsed.rejected == 1


def test_a_number_sent_as_text_is_read():
    assert values(own(s(STEPS, ' 9200 '))) == {('steps', DAY): 9200.0}


def test_types_this_server_does_not_read_are_named_and_bounded():
    parsed = own(s('HKQuantityTypeIdentifierHeartRate', 70), s(None, 1), s(42, 1),
                 s('x' * 200, 1), s('bad\x00name\n', 1), 'not an object',
                 s(STEPS, 10))
    assert parsed.unrecognized == sorted(
        ['HKQuantityTypeIdentifierHeartRate', '(no name)', 'x' * 64, 'badname'])
    assert parsed.rejected == 1
    assert values(parsed) == {('steps', DAY): 10.0}

    many = own(*[s(f'Type{n:03d}', 1) for n in range(50)])
    assert len(many.unrecognized) == 20


# -- Health Auto Export's shape -------------------------------------------------

def test_health_auto_export_metrics_are_read_by_name():
    stamp = '2026-11-20 00:00:00 -0700'
    parsed = hae(
        metric('step_count', 'count', {'date': stamp, 'qty': 8123, 'source': 'Watch'}),
        metric('walking_running_distance', 'mi', {'date': stamp, 'qty': 4}),
        metric('active_energy', 'kJ', {'date': stamp, 'qty': 2092}),
        metric('Resting_Heart_Rate ', 'count/min', {'date': stamp, 'qty': 51}),
        metric('blood_oxygen_saturation', '%', {'date': stamp, 'qty': 96}),
        metric('weight_body_mass', 'lb', {'date': stamp, 'qty': 154}),
    )
    assert parsed.unrecognized == [] and parsed.rejected == 0
    assert values(parsed) == {
        ('steps', DAY): 8123.0, ('distance', DAY): 6.44, ('active_energy', DAY): 500.0,
        ('resting_hr', DAY): 51.0, ('spo2', DAY): 96.0, ('body_mass', DAY): 69.85,
    }


def test_health_auto_export_without_the_data_wrapper():
    parsed = hae(metric('step_count', 'count', {'date': '2026-11-20 00:00:00 +0100', 'qty': 5}),
                 wrapped=False)
    assert values(parsed) == {('steps', DAY): 5.0}


def test_every_health_auto_export_name_leads_to_a_mapped_type():
    types = {**_APPLE_TYPE_MAP, _APPLE_SLEEP_TYPE: 'sleep_duration'}
    assert set(api._HAE_NAME_TO_TYPE.values()) == set(types)


def test_a_summarised_night_is_its_total_sleep():
    stamp = '2026-11-20 00:00:00 -0700'
    parsed = hae(metric('sleep_analysis', 'hr',
                        {'date': stamp, 'totalSleep': 6.75, 'asleep': 0, 'core': 4, 'deep': 1,
                         'rem': 1.75, 'awake': 0.5, 'inBed': 8.1}))
    assert values(parsed) == {('sleep_duration', DAY): 6.75}
    # An older watch reports only the undivided figure.
    parsed = hae(metric('sleep_analysis', 'hr', {'date': stamp, 'totalSleep': 0, 'asleep': 7.2}))
    assert values(parsed) == {('sleep_duration', DAY): 7.2}
    # Failing both, the stages themselves.
    parsed = hae(metric('sleep_analysis', 'hr',
                        {'date': stamp, 'asleep': 0, 'core': 4, 'deep': 1, 'rem': 1.5}))
    assert values(parsed) == {('sleep_duration', DAY): 6.5}


def test_a_night_in_bed_with_no_sleep_recorded_is_not_a_night_of_no_sleep():
    """A phone with no watch records time in bed only. Zero hours would be
    stored, replace the day and pull the averages down."""
    parsed = hae(metric('sleep_analysis', 'hr',
                        {'date': '2026-11-20 00:00:00 -0700', 'totalSleep': 0, 'asleep': 0,
                         'inBed': 7.4}))
    assert parsed.samples == [] and parsed.rejected == 0


def test_unsummarised_sleep_counts_the_segments_asleep():
    def segment(stage, hours, start='2026-11-20 01:00:00 -0700'):
        return {'startDate': start, 'endDate': start, 'qty': hours, 'value': stage}
    parsed = hae(metric('sleep_analysis', 'hr',
                        segment('Core', 4), segment('Deep', 1), segment('REM', 1.5),
                        segment('Awake', 0.4), segment('In Bed', 8), segment('Asleep', 0.25)))
    assert values(parsed) == {('sleep_duration', DAY): 6.75}
    assert parsed.rejected == 0


def test_metrics_this_server_does_not_read_are_named():
    stamp = '2026-11-20 00:00:00 -0700'
    parsed = hae(metric('heart_rate', 'count/min', {'date': stamp, 'Min': 48, 'Avg': 70, 'Max': 131}),
                 metric('dietary_energy', 'kcal', {'date': stamp, 'qty': 2100}),
                 metric('walking_running_distance', 'furlong', {'date': stamp, 'qty': 3}),
                 metric('step_count', 'count', {'date': stamp, 'qty': 9200}, {'date': stamp}, 7),
                 'not a metric')
    assert parsed.unrecognized == ['dietary_energy', 'heart_rate']
    assert parsed.rejected == 4      # the furlongs, the point with no qty, the 7, the string
    assert values(parsed) == {('steps', DAY): 9200.0}


# -- bodies that are not read at all ---------------------------------------------

@pytest.mark.parametrize('body', [
    None, [], 'steps', 7, {}, {'data': {}}, {'data': []}, {'workouts': []},
    {'samples': {}}, {'samples': 'steps'}, {'data': {'metrics': {}}}, {'metrics': 'steps'},
])
def test_a_body_of_neither_shape_is_a_400(body):
    with pytest.raises(api.AppleHealthPayloadError) as raised:
        api.parse_payload(body, TODAY)
    assert raised.value.status == 400 and raised.value.message


def test_an_empty_list_is_a_request_with_nothing_in_it():
    assert own() == api.ParsedPayload(samples=[], unrecognized=[], rejected=0)
    assert hae() == api.ParsedPayload(samples=[], unrecognized=[], rejected=0)


def test_more_points_than_the_limit_is_a_413(monkeypatch):
    monkeypatch.setattr(api, 'MAX_POINTS', 3)
    assert len(own(*[s(STEPS, 1)] * 3).samples) == 1
    with pytest.raises(api.AppleHealthPayloadError) as raised:
        own(*[s(STEPS, 1)] * 4)
    assert raised.value.status == 413
    with pytest.raises(api.AppleHealthPayloadError):
        hae(metric('step_count', 'count', *[{'date': '2026-11-20', 'qty': 1}] * 4))
    # Names nobody reads count too: the limit is on work, not on what is kept.
    with pytest.raises(api.AppleHealthPayloadError):
        hae(*[metric(f'unknown_{n}', 'x') for n in range(4)])


@pytest.mark.parametrize('declared, over', [
    (None, False), ('', False), ('junk', False), ('0', False),
    (str(api.MAX_BODY_BYTES), False), (str(api.MAX_BODY_BYTES + 1), True),
])
def test_the_body_ceiling_reads_content_length(declared, over):
    assert api.too_large(declared) is over


# -- the tables agree with the rest of the wearable code ---------------------------

def test_unit_tables_cover_known_metrics_and_hold_the_stored_unit():
    for metric_key, factors in api._UNIT_FACTORS.items():
        assert metric_key in WEARABLE_CONCEPT_CODE
        assert 1.0 in factors.values(), metric_key
        assert all(unit == unit.lower().strip() for unit in factors), metric_key


def test_percent_metrics_are_bounded_as_percentages():
    for metric_key in api._PERCENT_METRICS:
        low, high = WEARABLE_ARTIFACT_BOUNDS[metric_key]
        assert low > 1.0 and high <= 100.0
