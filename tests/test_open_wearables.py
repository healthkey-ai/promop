"""The Open Wearables SDK protocol, served without its server (#2810).

The credentials, the reading of a sync body and the running day totals. The
routes and what a sync writes to a patient's record are in
``patient_portal.tests.OpenWearablesEndpointTest``.
"""
import uuid
from datetime import date, datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import pytest
from django.utils import timezone

from omop_core.models import OpenWearablesConnection, OpenWearablesDay
from omop_core.services import open_wearables as ow
from omop_core.services.wearable_parsers import WearableSample
from tests.factories import PersonFactory

TODAY = date(2026, 11, 23)
STEPS = 'HKQuantityTypeIdentifierStepCount'


def record(kind=STEPS, value=100, start='2026-11-20T18:00:00Z', unit='count', **more):
    return {'id': more.pop('id', f'{kind}-{start}-{value}'), 'type': kind, 'startDate': start,
            'endDate': more.pop('end', start), 'value': value, 'unit': unit, **more}


def read(records=(), sleep=(), device=ow.APPLE, zone='', workouts=()):
    return ow.read_sync({'provider': 'apple', 'data': {
        'records': list(records), 'sleep': list(sleep), 'workouts': list(workouts)}},
        device, time_zone=zone, today=TODAY)


def values(batch):
    return [(r.metric_key, r.day.isoformat(), r.value) for r in batch.readings]


# ── Credentials ─────────────────────────────────────────────────────────────


def test_a_code_is_eight_unambiguous_characters_and_its_digest_is_not_the_code():
    code, digest = ow.new_code()
    assert len(code) == 8 and set(code) <= set(ow.CODE_ALPHABET)
    assert not set('01OI') & set(ow.CODE_ALPHABET)
    assert digest != code and len(digest) == 64
    assert ow.code_digest(code) == digest == ow.code_digest(f'  {code.lower()} ')


@pytest.mark.parametrize('value', ['', 'ABC', 'ABCDEFG0', 'ABCDEFGHJ', 'ABCD EFG', None, 12345678, ['A' * 8]])
def test_what_cannot_be_a_code_has_no_digest(value):
    assert ow.code_digest(value) is None


def test_a_codes_digest_needs_this_deployments_key(settings):
    code, digest = ow.new_code()
    settings.SECRET_KEY = 'another-deployment'
    assert ow.code_digest(code) != digest


def test_a_refresh_token_is_stored_as_a_digest_and_only_its_own_shape_has_one():
    token, digest = ow.new_refresh_token()
    assert token.startswith('owr_') and digest not in token and len(digest) == 64
    assert ow.refresh_digest(token) == digest
    for other in ('', 'owr_short', token[4:], None, 7, token + '\n'):
        assert ow.refresh_digest(other) is None


def device(user_id=None, pk=7):
    return SimpleNamespace(pk=pk, connection=SimpleNamespace(user_id=user_id or uuid.uuid4()))


def test_an_access_token_names_its_phone_and_user_and_nothing_else_reads_as_one(settings):
    phone = device()
    token = ow.access_token(phone)
    assert ow.read_access_token(token) == (phone.connection.user_id, 7)
    for other in ('', 'x' * 30, token[:-2], token + 'A', None, 5):
        assert ow.read_access_token(other) is None
    settings.SECRET_KEY = 'another-deployment'
    assert ow.read_access_token(token) is None


def test_an_access_token_stops_working_after_an_hour(monkeypatch):
    token = ow.access_token(device())
    assert ow.ACCESS_TOKEN_SECONDS == 3600
    monkeypatch.setattr(ow, 'ACCESS_TOKEN_SECONDS', -1)
    assert ow.read_access_token(token) is None


@pytest.mark.parametrize('name, kept', [
    ('America/Denver', 'America/Denver'), ('UTC', 'UTC'), ('Mars/Olympus', ''),
    ('../../etc/passwd', ''), ('', ''), (None, ''), (5, ''), ('A' * 65, ''),
])
def test_a_time_zone_is_kept_only_when_it_is_one(name, kept):
    assert ow.clean_time_zone(name) == kept


def test_it_is_on_unless_the_deployment_switches_it_off(settings):
    assert ow.enabled()
    settings.OPEN_WEARABLES_SYNC_ENABLED = False
    assert not ow.enabled()


# ── Reading a sync body ─────────────────────────────────────────────────────


def test_healthkit_records_are_read_by_their_identifier():
    batch = read([
        record(STEPS, 1200),
        record('HKQuantityTypeIdentifierRestingHeartRate', 58, unit='bpm'),
        record('HKQuantityTypeIdentifierHeartRateVariabilitySDNN', 44, unit='ms'),
        record('HKQuantityTypeIdentifierVO2Max', 41.5, unit='mL/kg/min'),
    ])
    assert values(batch) == [
        ('steps', '2026-11-20', 1200.0), ('resting_hr', '2026-11-20', 58.0),
        ('hrv_sdnn', '2026-11-20', 44.0), ('vo2_max', '2026-11-20', 41.5),
    ]
    assert batch.rejected == 0 and batch.unrecognized == []
    assert batch.newest == datetime(2026, 11, 20, 18, 0, tzinfo=dt_timezone.utc)


def test_health_connect_records_are_read_by_the_android_sdks_names():
    batch = read([
        record('STEP_COUNT', 900),
        record('HEART_RATE_VARIABILITY', 38, unit='ms'),
        record('WEIGHT', 70.5, unit='kg'),
        record('ACTIVE_CALORIES_BURNED', 410, unit='kcal'),
        record(STEPS, 5),                       # an iPhone's name means nothing here
        # Walking, running and cycling together: not the walking-and-running metric.
        record('DISTANCE', 2500, unit='m'),
    ], device=ow.ANDROID)
    assert values(batch) == [
        ('steps', '2026-11-20', 900.0),
        # Health Connect's HRV is RMSSD, never SDNN (#438).
        ('hrv_rmssd', '2026-11-20', 38.0),
        ('body_mass', '2026-11-20', 70.5),
        ('active_energy', '2026-11-20', 410.0),
    ]
    assert batch.unrecognized == ['DISTANCE', STEPS]


@pytest.mark.parametrize('start, end', [
    ('0001-01-01T00:00:00Z', '0001-01-01T00:00:00Z'),      # HealthKit's distant past
    ('9999-12-31T23:59:59Z', '9999-12-31T23:59:59Z'),
    ('4001-01-01T00:00:00Z', '4001-01-01T00:00:00Z'),      # ... and distant future
])
def test_a_date_no_calendar_can_hold_rejects_its_record_and_not_the_batch(start, end):
    for zone in ('', 'America/New_York', 'Asia/Tokyo'):
        batch = read([record(start=start, end=end), record(STEPS, 7, id='fine')],
                     sleep=[stage('deep', start, end, sid='s')], zone=zone)
        assert [r.value for r in batch.readings] == [7.0], zone
        assert batch.rejected == 2, zone


def test_an_end_date_in_the_distant_future_does_not_become_how_far_the_sync_has_got():
    batch = read([record(STEPS, 5, start='2026-11-20T18:00:00Z', end='4001-01-01T00:00:00Z')])
    assert [r.value for r in batch.readings] == [5.0]
    assert batch.newest is None
    both = read([record(STEPS, 5, start='2026-11-20T18:00:00Z', end='4001-01-01T00:00:00Z', id='far'),
                 record(STEPS, 6, start='2026-11-21T09:00:00Z', id='near')])
    assert both.newest == datetime(2026, 11, 21, 9, 0, tzinfo=dt_timezone.utc)


def test_an_id_that_is_not_valid_unicode_is_still_an_id():
    batch = read([record(STEPS, 5, id='\ud83d'), record(STEPS, 6, id='\ud83d')])
    assert len(batch.readings) == 2 and batch.readings[0].key == batch.readings[1].key
    assert batch.rejected == 0


def test_something_unforeseen_in_one_record_costs_that_record_only(monkeypatch):
    real = ow._Reader._key

    def explode(self, record, *args):
        if record.get('id') == 'boom':
            raise RuntimeError('unforeseen')
        return real(self, record, *args)

    monkeypatch.setattr(ow._Reader, '_key', explode)
    batch = read([record(STEPS, 1, id='a'), record(STEPS, 2, id='boom'), record(STEPS, 3, id='c')])
    assert [r.value for r in batch.readings] == [1.0, 3.0] and batch.rejected == 1


@pytest.mark.parametrize('kind, value, unit, stored', [
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 1500, 'm', 1.5),
    ('HKQuantityTypeIdentifierDistanceWalkingRunning', 2, 'mi', 3.218688),
    ('HKQuantityTypeIdentifierWalkingSpeed', 1.2, 'm/s', 4.32),
    ('HKQuantityTypeIdentifierWalkingStepLength', 0.7, 'm', 70.0),
    ('HKQuantityTypeIdentifierActiveEnergyBurned', 300, 'Cal', 300.0),
    ('HKQuantityTypeIdentifierActiveEnergyBurned', 300, 'kcal', 300.0),
    ('HKQuantityTypeIdentifierBodyMass', 154, 'lb', 69.85322498),
    ('HKQuantityTypeIdentifierAppleExerciseTime', 1800, 's', 30.0),
])
def test_a_metric_with_more_than_one_unit_is_converted(kind, value, unit, stored):
    [reading] = read([record(kind, value, unit=unit)]).readings
    assert reading.value == pytest.approx(stored)


@pytest.mark.parametrize('unit', ['furlong', 'cal', 'KM', '', None, 5])
def test_an_unknown_unit_is_refused_not_guessed(unit):
    batch = read([record('HKQuantityTypeIdentifierDistanceWalkingRunning', 3, unit=unit)])
    assert batch.readings == [] and batch.rejected == 1


def test_a_fraction_is_a_percentage_and_a_percentage_stays_one():
    batch = read([
        record('HKQuantityTypeIdentifierOxygenSaturation', 0.97, unit='%', id='a'),
        record('HKQuantityTypeIdentifierOxygenSaturation', 96, unit='%', id='b'),
        record('HKQuantityTypeIdentifierWalkingDoubleSupportPercentage', 0.28, unit='%', id='c'),
    ])
    assert [r.value for r in batch.readings] == pytest.approx([97.0, 96.0, 28.0])


def test_the_general_heart_rate_and_other_unread_types_are_named_and_not_stored():
    batch = read([
        record('HKQuantityTypeIdentifierHeartRate', 71, unit='bpm'),
        record('HKQuantityTypeIdentifierBloodGlucose', 99, unit='mg/dL'),
        record(None, 1), record('', 1), record(7, 1), record('odd\x00type\n' + 'x' * 200, 1),
    ])
    assert batch.readings == []
    assert '(no type)' in batch.unrecognized
    assert 'HKQuantityTypeIdentifierHeartRate' in batch.unrecognized
    assert all(len(name) <= 64 and name.isprintable() for name in batch.unrecognized)
    assert batch.rejected == 0


@pytest.mark.parametrize('change', [
    {'value': 'many'}, {'value': None}, {'value': True}, {'value': float('inf')}, {'value': [1]},
    {'start': 'yesterday'}, {'start': None}, {'start': 20261120},
    {'start': '2013-12-31T12:00:00Z'},         # before HealthKit existed
    {'start': '2026-11-26T12:00:00Z'},         # further ahead than any zone
])
def test_a_record_without_a_usable_value_or_time_is_rejected(change):
    fields = {'kind': STEPS, 'value': 100, 'start': '2026-11-20T18:00:00Z', **change}
    batch = read([record(**fields)])
    assert batch.readings == [] and batch.rejected == 1


def test_things_that_are_not_records_are_rejected_and_the_rest_is_read():
    batch = read([record(STEPS, 10), 'text', 5, None, [], record(STEPS, 20, id='second')])
    assert [r.value for r in batch.readings] == [10.0, 20.0] and batch.rejected == 4


def test_the_day_is_where_the_person_was():
    late = '2026-11-21T05:30:00Z'       # 22:30 on the 20th in Denver
    assert values(read([record(start=late)])) == [('steps', '2026-11-21', 100.0)]
    assert values(read([record(start=late)], zone='America/Denver')) == [('steps', '2026-11-20', 100.0)]
    # The record's own offset, when HealthKit kept one, wins over the zone.
    assert values(read([record(start=late, zoneOffset='+09:00')], zone='America/Denver')) == [
        ('steps', '2026-11-21', 100.0)]
    assert values(read([record(start=late, zoneOffset='-0700')])) == [('steps', '2026-11-20', 100.0)]
    # Android writes UTC as "Z": an offset, not the absence of one.
    assert values(read([record(start=late, zoneOffset='Z')], zone='America/Denver')) == [
        ('steps', '2026-11-21', 100.0)]
    # An offset that is not one is no offset.
    assert values(read([record(start=late, zoneOffset='+25:00')], zone='America/Denver')) == [
        ('steps', '2026-11-20', 100.0)]
    assert values(read([record(start=late)], zone='Not/AZone')) == [('steps', '2026-11-21', 100.0)]


def test_tomorrow_in_a_later_zone_is_a_day_and_the_day_after_is_not():
    assert len(read([record(start='2026-11-24T02:00:00Z')]).readings) == 1
    assert read([record(start='2026-11-25T02:00:00Z')]).rejected == 1


def test_the_same_record_has_the_same_key_however_it_is_sent_again():
    one = read([record(STEPS, 100, id='HK-UUID-1')]).readings[0]
    again = read([record(STEPS, 100, id=' HK-UUID-1 ')]).readings[0]
    other = read([record(STEPS, 100, id='HK-UUID-2')]).readings[0]
    assert one.key == again.key != other.key and len(one.key) == 16
    # No id: the type, the times and the value stand in for it.
    plain = dict(kind=STEPS, value=100, start='2026-11-20T18:00:00Z')
    first = read([{**record(**plain), 'id': None}]).readings[0]
    same = read([{**record(**plain), 'id': ''}]).readings[0]
    later = read([{**record(**{**plain, 'start': '2026-11-20T18:05:00Z'}), 'id': None}]).readings[0]
    assert first.key == same.key != later.key


def stage(name, start, end, sid=None):
    return {'id': sid or f'{name}-{start}', 'stage': name, 'startDate': start, 'endDate': end}


def test_sleep_is_the_stages_asleep_by_the_day_each_started():
    batch = read(sleep=[
        stage('in_bed', '2026-11-20T04:00:00Z', '2026-11-20T12:00:00Z'),
        stage('sleeping', '2026-11-20T04:30:00Z', '2026-11-20T06:00:00Z'),
        stage('LIGHT', '2026-11-20T06:00:00Z', '2026-11-20T08:00:00Z'),
        stage('awake', '2026-11-20T08:00:00Z', '2026-11-20T08:20:00Z'),
        stage('deep', '2026-11-20T08:20:00Z', '2026-11-20T09:20:00Z'),
        stage('rem', '2026-11-20T09:20:00Z', '2026-11-20T10:05:00Z'),
        stage('unknown', '2026-11-20T10:05:00Z', '2026-11-20T10:30:00Z'),
    ])
    assert [r.metric_key for r in batch.readings] == ['sleep_duration'] * 4
    assert sum(r.value for r in batch.readings) == pytest.approx(5.25)
    assert batch.rejected == 0


@pytest.mark.parametrize('start, end', [
    ('2026-11-20T08:00:00Z', '2026-11-20T08:00:00Z'), ('2026-11-20T08:00:00Z', '2026-11-20T07:00:00Z'),
    ('2026-11-20T08:00:00Z', '2026-11-22T08:00:00Z'), ('2026-11-20T08:00:00Z', None), (None, None),
])
def test_a_sleep_stage_with_no_believable_length_is_rejected(start, end):
    batch = read(sleep=[stage('deep', start, end, sid='s')])
    assert batch.readings == [] and batch.rejected == 1


def test_only_what_is_read_counts_towards_the_limit(monkeypatch):
    """The SDK sends a refused batch again unchanged, so nothing it skips may refuse one."""
    monkeypatch.setattr(ow, 'MAX_ITEMS', 2)
    skipped = [record('HKQuantityTypeIdentifierHeartRate', 70 + i, unit='bpm', id=f'hr{i}') for i in range(50)]
    batch = read([record(STEPS, 1, id='a'), *skipped, record(STEPS, 2, id='b')],
                 sleep=[stage('awake', '2026-11-20T08:00:00Z', '2026-11-20T08:20:00Z')] * 20,
                 workouts=[{'type': 'RUNNING'}] * 20)
    assert [r.value for r in batch.readings] == [1.0, 2.0]
    with pytest.raises(ow.SyncError) as error:
        read([record(STEPS, i, id=f's{i}') for i in range(3)])
    assert error.value.status == 413


@pytest.mark.parametrize('body', [None, [], 'text', {}, {'data': None}, {'data': []}, {'provider': 'apple'}])
def test_a_body_with_no_data_object_is_refused(body):
    with pytest.raises(ow.SyncError) as error:
        ow.read_sync(body, ow.APPLE, today=TODAY)
    assert error.value.status == 400


@pytest.mark.parametrize('name', ['records', 'sleep'])
def test_a_data_field_that_is_not_a_list_is_refused(name):
    with pytest.raises(ow.SyncError):
        ow.read_sync({'data': {name: {'not': 'a list'}}}, ow.APPLE, today=TODAY)


def test_an_empty_sync_reads_as_nothing():
    batch = read()
    assert batch.readings == [] and batch.newest is None


def test_every_provider_the_sdk_names_is_read():
    # The Android SDK reads one store, and on a Galaxy phone that is Samsung
    # Health unless the app chose otherwise: discarding it would look connected
    # and store nothing.
    assert ow.PROVIDER_DEVICE == {'apple': 'apple', 'health_connect': 'android', 'google': 'android',
                                  'samsung': 'android'}
    assert ow.PROVIDER_SLUGS['google'] == 'health_connect'
    assert set(ow.PROVIDER_SLUGS) == set(ow.PROVIDER_DEVICE)
    assert all(len(device) <= 10 for device in ow.PROVIDER_DEVICE.values())    # WearableUpload.device_type


# ── Day totals ──────────────────────────────────────────────────────────────


@pytest.fixture
def connection(db):
    return OpenWearablesConnection.objects.create(person=PersonFactory(), user_id=uuid.uuid4())


def reading(metric='steps', day=date(2026, 11, 20), key='a', value=100.0):
    return ow.Reading(metric, day, key, value)


def test_a_days_readings_are_summed_or_averaged_as_the_export_parser_does(connection):
    applied = ow.apply_readings(connection, ow.APPLE, [
        reading(key='a', value=3000), reading(key='b', value=2500),
        reading('resting_hr', key='c', value=58), reading('resting_hr', key='d', value=61),
    ])
    assert applied.samples == [
        WearableSample('resting_hr', date(2026, 11, 20), 59.5),
        WearableSample('steps', date(2026, 11, 20), 5500.0),
    ]
    assert (applied.counted, applied.repeated, applied.late) == (4, 0, 0)


def test_a_reading_is_counted_once_across_batches_and_within_one(connection):
    ow.apply_readings(connection, ow.APPLE, [reading(key='a', value=3000)])
    applied = ow.apply_readings(connection, ow.APPLE, [
        reading(key='a', value=3000), reading(key='b', value=500), reading(key='b', value=500)])
    assert applied.samples == [WearableSample('steps', date(2026, 11, 20), 3500.0)]
    assert (applied.counted, applied.repeated) == (1, 2)
    row = OpenWearablesDay.objects.get()
    assert (row.total, row.count, row.seen) == (3500.0, 2, {'a': 3000.0, 'b': 500.0})


def test_a_record_updated_in_place_moves_the_day_by_the_difference(connection):
    """Health Connect lets a writer update a record: the same id, a new value."""
    ow.apply_readings(connection, ow.ANDROID, [reading(key='bucket', value=1200), reading(key='other', value=300)])
    applied = ow.apply_readings(connection, ow.ANDROID, [reading(key='bucket', value=4100)])
    assert applied.samples == [WearableSample('steps', date(2026, 11, 20), 4400.0)]
    assert (applied.counted, applied.repeated) == (1, 0)
    row = OpenWearablesDay.objects.get()
    assert (row.total, row.count) == (4400.0, 2)
    # And for an averaged metric the count does not grow.
    ow.apply_readings(connection, ow.ANDROID, [reading('resting_hr', key='r', value=60)])
    mean = ow.apply_readings(connection, ow.ANDROID, [reading('resting_hr', key='r', value=64)])
    assert mean.samples == [WearableSample('resting_hr', date(2026, 11, 20), 64.0)]


def test_a_batch_of_nothing_new_changes_nothing(connection):
    ow.apply_readings(connection, ow.APPLE, [reading(key='a')])
    before = OpenWearablesDay.objects.get().updated_at
    applied = ow.apply_readings(connection, ow.APPLE, [reading(key='a')])
    assert applied.samples == [] and applied.repeated == 1
    assert OpenWearablesDay.objects.get().updated_at == before
    assert ow.apply_readings(connection, ow.APPLE, []).samples == []


def test_each_device_and_each_connection_keeps_its_own_days(connection, db):
    other = OpenWearablesConnection.objects.create(person=PersonFactory(), user_id=uuid.uuid4())
    ow.apply_readings(connection, ow.APPLE, [reading(key='a', value=100)])
    android = ow.apply_readings(connection, ow.ANDROID, [reading(key='a', value=40)])
    theirs = ow.apply_readings(other, ow.APPLE, [reading(key='a', value=7)])
    assert android.samples[0].value == 40.0 and theirs.samples[0].value == 7.0
    assert OpenWearablesDay.objects.count() == 3


def test_a_day_holds_only_so_many_records(connection, monkeypatch):
    monkeypatch.setattr(ow, 'MAX_SEEN_PER_DAY', 2)
    applied = ow.apply_readings(connection, ow.APPLE, [
        reading(key='a', value=1), reading(key='b', value=2), reading(key='c', value=4)])
    assert applied.samples[0].value == 3.0 and applied.late == 1


def test_a_day_nothing_arrives_for_is_closed_and_then_ignores_records(connection):
    now = timezone.now()
    ow.apply_readings(connection, ow.APPLE, [reading(key='a', value=3000)], now - ow.CLOSE_AFTER - timedelta(days=1))
    ow.apply_readings(connection, ow.APPLE, [reading(day=date(2026, 11, 22), key='z', value=10)], now)
    assert ow.close_idle_days(connection, now) == 1
    old = OpenWearablesDay.objects.get(day=date(2026, 11, 20))
    assert (old.closed, old.seen, old.total) == (True, {}, 3000.0)
    assert not OpenWearablesDay.objects.get(day=date(2026, 11, 22)).closed
    # A re-export after the keys are gone cannot be told from new data, so it is not counted.
    applied = ow.apply_readings(connection, ow.APPLE, [reading(key='a', value=3000), reading(key='n', value=5)], now)
    assert applied.samples == [] and applied.late == 2
    assert OpenWearablesDay.objects.get(day=date(2026, 11, 20)).total == 3000.0


def test_a_day_well_in_the_past_is_closed_a_day_after_its_records_stop(connection):
    """A backfill sends an old day together, so its keys are not kept for weeks."""
    now = timezone.now()
    today = now.date()
    for day, age in ((today - timedelta(days=30), timedelta(hours=30)),      # old and quiet: closed
                     (today - timedelta(days=30, hours=24), timedelta(hours=2)),   # old, still arriving
                     (today - timedelta(days=2), timedelta(hours=30))):      # recent: may still grow
        ow.apply_readings(connection, ow.APPLE, [reading(day=day, key=f'k{day}', value=1)], now - age)
    assert ow.close_idle_days(connection, now) == 1
    assert list(OpenWearablesDay.objects.filter(closed=True).values_list('day', flat=True)) == [
        today - timedelta(days=30)]
    # With no connection named, every connection's: the command for phones that never sync again.
    assert ow.close_idle_days(now=now + timedelta(days=60)) == 2


def test_the_query_count_does_not_grow_with_the_readings(connection, django_assert_max_num_queries):
    many = [reading(key=f'k{i}', day=date(2026, 11, 1) + timedelta(days=i % 20), value=1) for i in range(400)]
    with django_assert_max_num_queries(3):
        ow.apply_readings(connection, ow.APPLE, many)
    more = [reading(key=f'm{i}', day=date(2026, 11, 1) + timedelta(days=i % 20), value=1) for i in range(400)]
    with django_assert_max_num_queries(3):
        ow.apply_readings(connection, ow.APPLE, more)
