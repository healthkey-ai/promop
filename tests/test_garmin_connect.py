"""The Garmin Connect Personal API reader (#2794).

Garmin's own client makes the data calls; only the HTTP session is faked, so
the paths, parameters and headers asserted here are the ones the client really
sends. No test needs a database.
"""
import json
import logging
from datetime import date, timedelta

import pytest
import requests

from omop_core.services import garmin_connect as gc
from omop_core.services.wearable_parsers import WearableSample

KEY = 'gcpk_TEST-key.0123456789'
TOKEN = 'aaa.bbb.ccc'
BASE = 'https://personal.connectapi.garmin.com'
START, END = date(2026, 11, 1), date(2026, 11, 7)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self.ok = status < 400
        self.text = text if text is not None else ('' if payload is None else json.dumps(payload))
        self.content = self.text.encode()

    def json(self):
        return json.loads(self.text)


def summary(day, **over):
    record = {
        'calendarDate': day,
        'includesWellnessData': True,
        'movement': {'steps': {'value': 8000, 'distanceInMeters': 6400.0}},
        'calories': {'burnedActive': 450, 'burnedResting': 1700},
        'heartRate': {'restingValue': 52},
        'intensityMinutes': {'moderate': 20, 'vigorous': 10},
        'respiration': {'avgValue': 14},
        'pulseOx': {'avgValue': 96},
    }
    record.update(over)
    return record


class FakeSession:
    """Answers by URL path. A route is a response, an exception, or a callable
    taking the request's params and returning either."""

    def __init__(self, **routes):
        self.routes = {
            'token': FakeResponse(text=TOKEN),
            'dailysummaries': FakeResponse(payload={'data': []}),
            'hrv': FakeResponse(status=204),
            'sleep': FakeResponse(payload=[]),
            'maxmet': FakeResponse(payload=[]),
        }
        self.routes.update(routes)
        self.posts, self.gets, self.closed = [], [], False

    def _answer(self, name, params=None):
        answer = self.routes[name]
        if callable(answer):
            answer = answer(params)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, url, headers=None, timeout=None, allow_redirects=True):
        self.posts.append({'url': url, 'headers': headers, 'timeout': timeout,
                           'allow_redirects': allow_redirects})
        return self._answer('token')

    def get(self, url, headers=None, params=None, timeout=None):
        self.gets.append({'url': url, 'headers': headers, 'params': params, 'timeout': timeout})
        for name in ('dailysummaries', 'hrv', 'sleep', 'maxmet'):
            if name in url:
                return self._answer(name, {'url': url, 'params': params})
        raise AssertionError(f'unexpected GET {url}')

    def close(self):
        self.closed = True


def fetch(session, start=START, end=END, **kwargs):
    return gc.fetch_daily_samples(KEY, start, end, session=session, **kwargs)


def by_metric(samples):
    return {(s.metric_key, s.date.isoformat()): s.value for s in samples}


# -- the key and the range ----------------------------------------------------

@pytest.mark.parametrize('raw', [None, '', '   ', 42, ['k'], {'k': 1}])
def test_a_missing_key_is_a_400(raw):
    with pytest.raises(gc.GarminSyncError) as refused:
        gc.clean_api_key(raw)
    assert refused.value.status == 400


@pytest.mark.parametrize('raw', ['two words', 'line\nbreak', 'tab\there', 'café', 'x' * 4097,
                                 'key\r\nX-Injected: 1'])
def test_a_key_that_cannot_be_a_header_value_is_a_400(raw):
    with pytest.raises(gc.GarminSyncError) as refused:
        gc.clean_api_key(raw)
    assert refused.value.status == 400
    assert raw not in refused.value.message


def test_a_key_is_trimmed():
    assert gc.clean_api_key(f'  {KEY}\n') == KEY


def test_the_range_defaults_to_thirty_days_ending_yesterday():
    today = date(2026, 11, 23)
    assert gc.clean_range(None, None, today) == (date(2026, 10, 24), date(2026, 11, 22))
    assert gc.clean_range('', '', today) == (date(2026, 10, 24), date(2026, 11, 22))


def test_the_range_takes_days_and_an_end_date():
    today = date(2026, 11, 23)
    assert gc.clean_range(7, '2026-11-20', today) == (date(2026, 11, 14), date(2026, 11, 20))
    assert gc.clean_range('1', '2026-11-20', today) == (date(2026, 11, 20), date(2026, 11, 20))
    assert gc.clean_range(90, None, today)[0] == date(2026, 8, 25)
    # A caller ahead of the server's clock may name its own today.
    assert gc.clean_range(1, '2026-11-24', today) == (date(2026, 11, 24), date(2026, 11, 24))


@pytest.mark.parametrize('days', [0, -1, 91, 'many', '7.5', 7.5, True, [7]])
def test_days_outside_one_to_ninety_is_a_400(days):
    with pytest.raises(gc.GarminSyncError) as refused:
        gc.clean_range(days, None, date(2026, 11, 23))
    assert refused.value.status == 400


@pytest.mark.parametrize('end_date', ['2026-11-25', 'yesterday', '2026-13-01', 20261120, ['2026-11-20']])
def test_a_bad_or_future_end_date_is_a_400(end_date):
    with pytest.raises(gc.GarminSyncError) as refused:
        gc.clean_range(7, end_date, date(2026, 11, 23))
    assert refused.value.status == 400


# -- the calls ---------------------------------------------------------------

def test_the_key_buys_a_token_and_the_token_reads_the_data():
    session = FakeSession()
    fetch(session)

    assert len(session.posts) == 1
    post = session.posts[0]
    assert post['url'] == f'{BASE}/personalapitoken-service/personal-api/personal-api-key/token'
    assert post['headers']['Authorization'] == f'ApiKey {KEY}'
    assert post['allow_redirects'] is False
    assert 0 < post['timeout'] <= gc.REQUEST_TIMEOUT_SECONDS

    assert session.gets, 'no data call was made'
    for get in session.gets:
        assert get['url'].startswith(BASE + '/')
        assert get['headers']['Authorization'] == f'Bearer {TOKEN}'
        assert KEY not in json.dumps(get, default=str)
        assert 0 < get['timeout'] <= gc.REQUEST_TIMEOUT_SECONDS
    # A session the caller passed in is the caller's to close.
    assert session.closed is False


def test_each_data_set_is_asked_for_the_range():
    session = FakeSession()
    fetch(session)
    urls = [(g['url'].removeprefix(BASE), g['params']) for g in session.gets]
    assert urls == [
        ('/usersummary-service/v2/dailysummaries', {'startDate': '2026-11-01', 'endDate': '2026-11-07'}),
        ('/hrv-service/hrv/daily/2026-11-01/2026-11-07', None),
        ('/sleep-service/sleep/dailySleepSummariesByDate',
         {'startDate': '2026-11-01', 'endDate': '2026-11-07'}),
        ('/metrics-service/metrics/maxmet/daily/2026-11-01/2026-11-07', None),
    ]


def test_ninety_days_is_one_summary_call_and_28_day_chunks_for_the_rest():
    session = FakeSession()
    end = date(2026, 11, 22)
    fetch(session, start=end - timedelta(days=89), end=end)

    summaries = [g for g in session.gets if 'dailysummaries' in g['url']]
    assert [g['params'] for g in summaries] == [{'startDate': '2026-08-25', 'endDate': '2026-11-22'}]

    sleep = [g['params'] for g in session.gets if 'sleep-service' in g['url']]
    assert sleep == [
        {'startDate': '2026-08-25', 'endDate': '2026-09-21'},
        {'startDate': '2026-09-22', 'endDate': '2026-10-19'},
        {'startDate': '2026-10-20', 'endDate': '2026-11-16'},
        {'startDate': '2026-11-17', 'endDate': '2026-11-22'},
    ]
    assert len([g for g in session.gets if 'hrv-service' in g['url']]) == 4
    assert len([g for g in session.gets if 'maxmet' in g['url']]) == 4


def test_chunks_cover_the_range_once():
    start, end = date(2026, 1, 1), date(2026, 3, 31)
    chunks = list(gc._chunks(start, end))
    assert chunks[0][0] == start and chunks[-1][1] == end
    assert all((b - a).days < gc.CHUNK_DAYS for a, b in chunks)
    assert all(chunks[i][1] + timedelta(days=1) == chunks[i + 1][0] for i in range(len(chunks) - 1))
    assert list(gc._chunks(start, start)) == [(start, start)]


# -- reading the answers -----------------------------------------------------------

def test_a_daily_summary_becomes_one_sample_per_metric():
    session = FakeSession(dailysummaries=FakeResponse(payload={'data': [summary('2026-11-03')]}))
    samples, failed = fetch(session)
    assert failed == []
    assert all(isinstance(s, WearableSample) for s in samples)
    assert by_metric(samples) == {
        ('steps', '2026-11-03'): 8000.0,
        ('distance', '2026-11-03'): 6.4,           # metres to km
        ('active_minutes', '2026-11-03'): 30.0,    # moderate + vigorous
        ('active_energy', '2026-11-03'): 450.0,
        ('basal_energy', '2026-11-03'): 1700.0,
        ('resting_hr', '2026-11-03'): 52.0,
        ('respiratory_rate', '2026-11-03'): 14.0,
        ('spo2', '2026-11-03'): 96.0,
    }


def test_hrv_sleep_and_vo2_max_are_read():
    session = FakeSession(
        hrv=FakeResponse(payload={'hrvSummaries': [
            {'calendarDate': '2026-11-02', 'lastNightAvg': 48, 'weeklyAvg': 51},
            {'calendarDate': '2026-11-03', 'lastNightAvg': None, 'weeklyAvg': 51},
        ]}),
        sleep=FakeResponse(payload=[
            {'calendarDate': '2026-11-02', 'sleepTimeSeconds': 27000, 'napTimeSeconds': 1800},
            {'calendarDate': '2026-11-03', 'sleepTimeSeconds': 0},
        ]),
        maxmet=FakeResponse(payload=[
            {'generic': {'calendarDate': '2026-11-02', 'vo2MaxPreciseValue': 47.6, 'vo2MaxValue': 48.0},
             'cycling': {'calendarDate': '2026-11-02', 'vo2MaxPreciseValue': 51.0}},
            {'generic': None, 'cycling': {'calendarDate': '2026-11-04', 'vo2MaxValue': 50.0}},
            {'generic': {'calendarDate': '2026-11-05', 'vo2MaxPreciseValue': None, 'vo2MaxValue': 47.0}},
        ]),
    )
    samples, failed = fetch(session)
    assert failed == []
    assert by_metric(samples) == {
        ('hrv_rmssd', '2026-11-02'): 48.0,
        ('sleep_duration', '2026-11-02'): 7.5,     # the night, naps apart
        ('vo2_max', '2026-11-02'): 47.6,           # running/walking before cycling
        ('vo2_max', '2026-11-04'): 50.0,
        ('vo2_max', '2026-11-05'): 47.0,
    }


def test_a_day_without_all_day_data_is_not_a_day_of_zero_steps():
    session = FakeSession(dailysummaries=FakeResponse(payload={'data': [
        summary('2026-11-02', includesWellnessData=False,
                movement={'steps': {'value': 0, 'distanceInMeters': 0.0}}),
        summary('2026-11-03'),
    ]}))
    samples, _ = fetch(session)
    assert {s.date.isoformat() for s in samples} == {'2026-11-03'}


def test_missing_and_malformed_values_are_left_out():
    session = FakeSession(dailysummaries=FakeResponse(payload={'data': [
        {'calendarDate': '2026-11-02'},
        summary('2026-11-03', movement=None, calories='n/a', heartRate={'restingValue': 0},
                intensityMinutes={'moderate': None, 'vigorous': None},
                respiration={'avgValue': True}, pulseOx={'avgValue': 'high'}),
        summary('2026-11-04', intensityMinutes={'moderate': 12, 'vigorous': None}),
        summary('not-a-date'),
        summary(None),
        'not a record',
    ]}))
    samples, _ = fetch(session)
    found = by_metric(samples)
    assert not [key for key in found if key[1] in ('2026-11-02', '2026-11-03')]
    assert found[('active_minutes', '2026-11-04')] == 12.0
    assert {key[1] for key in found} == {'2026-11-04'}


def test_days_outside_the_range_are_left_out():
    session = FakeSession(dailysummaries=FakeResponse(payload={'data': [
        summary('2026-10-31'), summary('2026-11-01'), summary('2026-11-07'), summary('2026-11-08'),
    ]}))
    samples, _ = fetch(session)
    assert {s.date.isoformat() for s in samples} == {'2026-11-01', '2026-11-07'}


@pytest.mark.parametrize('payload', [None, [], {'data': None}, {'data': 'x'}, 'text', 7])
def test_an_answer_of_another_shape_is_no_data(payload):
    session = FakeSession(
        dailysummaries=FakeResponse(payload=payload), hrv=FakeResponse(payload=payload),
        sleep=FakeResponse(payload=payload), maxmet=FakeResponse(payload=payload))
    assert fetch(session) == ([], [])


def test_samples_come_back_in_date_then_metric_order():
    session = FakeSession(dailysummaries=FakeResponse(payload={'data': [
        summary('2026-11-05'), summary('2026-11-02')]}))
    samples, _ = fetch(session)
    assert samples == sorted(samples, key=lambda s: (s.date, s.metric_key))
    assert samples[0].date == date(2026, 11, 2)


# -- failures -------------------------------------------------------------------

@pytest.mark.parametrize('status,expected', [(401, 400), (403, 400), (429, 429), (500, 502), (404, 502)])
def test_a_refused_key_exchange_ends_the_sync(status, expected):
    session = FakeSession(token=FakeResponse(status=status, text='{"error":"secret detail"}'))
    with pytest.raises(gc.GarminSyncError) as refused:
        fetch(session)
    assert refused.value.status == expected
    assert 'secret detail' not in refused.value.message
    assert session.gets == []


@pytest.mark.parametrize('text', ['', '   ', 'two words', 'x' * 20000])
def test_a_token_answer_that_is_not_a_token_is_a_502(text):
    with pytest.raises(gc.GarminSyncError) as refused:
        fetch(FakeSession(token=FakeResponse(text=text)))
    assert refused.value.status == 502


@pytest.mark.parametrize('error,expected', [
    (requests.Timeout('slow'), 504), (requests.ConnectionError('down'), 502)])
def test_a_token_request_that_fails_ends_the_sync(error, expected):
    with pytest.raises(gc.GarminSyncError) as refused:
        fetch(FakeSession(token=error))
    assert refused.value.status == expected


@pytest.mark.parametrize('answer,expected', [
    (FakeResponse(status=401), 400), (FakeResponse(status=403), 400),
    (FakeResponse(status=429), 429), (FakeResponse(status=500), 502),
    (FakeResponse(text='<html>not json</html>'), 502),
    (requests.Timeout('slow'), 504), (requests.ConnectionError('down'), 502),
])
def test_the_daily_summaries_are_required(answer, expected):
    with pytest.raises(gc.GarminSyncError) as refused:
        fetch(FakeSession(dailysummaries=answer))
    assert refused.value.status == expected


@pytest.mark.parametrize('answer', [
    FakeResponse(status=500), FakeResponse(status=404), FakeResponse(text='not json'),
    requests.Timeout('slow'), requests.ConnectionError('down'),
])
def test_an_optional_data_set_that_fails_is_reported_and_the_rest_kept(answer):
    session = FakeSession(
        dailysummaries=FakeResponse(payload={'data': [summary('2026-11-03')]}),
        sleep=answer,
        maxmet=FakeResponse(payload=[{'generic': {'calendarDate': '2026-11-03', 'vo2MaxValue': 45}}]),
    )
    samples, failed = fetch(session)
    assert failed == ['sleep']
    found = by_metric(samples)
    assert found[('steps', '2026-11-03')] == 8000.0
    assert found[('vo2_max', '2026-11-03')] == 45.0
    assert not [key for key in found if key[0] == 'sleep_duration']


@pytest.mark.parametrize('status,expected', [(401, 400), (403, 400), (429, 429)])
def test_a_refused_key_or_a_rate_limit_ends_the_sync_wherever_it_happens(status, expected):
    session = FakeSession(hrv=FakeResponse(status=status))
    with pytest.raises(gc.GarminSyncError) as refused:
        fetch(session)
    assert refused.value.status == expected
    assert not [g for g in session.gets if 'sleep-service' in g['url']]


def test_the_whole_read_has_a_time_budget():
    """Each call takes 7.3 seconds on a fake clock: the summaries and two more
    fit in 22, and the data set that does not is reported, not waited for."""
    now = [0.0]

    def slow(payload):
        def answer(_request):
            now[0] += 7.3
            return FakeResponse(payload=payload)
        return answer

    session = FakeSession(
        dailysummaries=slow({'data': [summary('2026-11-03')]}),
        hrv=slow({'hrvSummaries': []}), sleep=slow([]), maxmet=slow([]))
    samples, failed = fetch(session, clock=lambda: now[0])
    assert failed == ['vo2_max']
    assert by_metric(samples)[('steps', '2026-11-03')] == 8000.0
    # The last call that ran was given what was left, not the full allowance.
    assert session.gets[-1]['timeout'] == pytest.approx(7.4)


def test_a_budget_spent_before_the_summaries_is_a_504():
    now = [0.0]

    def slow_token(_request):
        now[0] += 30.0
        return FakeResponse(text=TOKEN)

    with pytest.raises(gc.GarminSyncError) as refused:
        fetch(FakeSession(token=slow_token), clock=lambda: now[0])
    assert refused.value.status == 504


def test_neither_the_key_nor_the_token_nor_garmins_text_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    session = FakeSession(
        dailysummaries=FakeResponse(payload={'data': [summary('2026-11-03')]}),
        sleep=FakeResponse(status=500, text='garmin-internal-detail'))
    fetch(session)
    with pytest.raises(gc.GarminSyncError):
        fetch(FakeSession(token=FakeResponse(status=401, text='garmin-internal-detail')))
    with pytest.raises(gc.GarminSyncError):
        fetch(FakeSession(dailysummaries=FakeResponse(status=403, text='garmin-internal-detail')))

    logged = '\n'.join(record.getMessage() for record in caplog.records)
    assert logged, 'the failures above should have logged something'
    assert KEY not in logged
    assert TOKEN not in logged
    assert 'garmin-internal-detail' not in logged


def test_a_session_this_module_opened_is_closed(monkeypatch):
    opened = []

    def session_factory():
        opened.append(FakeSession())
        return opened[-1]

    monkeypatch.setattr(requests, 'Session', session_factory)
    gc.fetch_daily_samples(KEY, START, END)
    assert opened[0].closed is True

    opened.clear()
    monkeypatch.setattr(requests, 'Session', lambda: opened.append(
        FakeSession(token=FakeResponse(status=401))) or opened[-1])
    with pytest.raises(gc.GarminSyncError):
        gc.fetch_daily_samples(KEY, START, END)
    assert opened[0].closed is True
