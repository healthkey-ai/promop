"""The Oura Ring export parser (#2889): file names, columns, aggregation, limits."""
import io
import zipfile
from datetime import date

import pytest

from omop_core.services import oura_parser
from omop_core.services.oura_parser import file_kind, parse_oura_export

ACTIVITY = (
    'id;day;steps;high_activity_time;medium_activity_time;active_calories\n'
    'a;2024-07-01;8200;600;1800;410\n'
    'b;2024-07-02;;;;\n'
    'c;not-a-day;5;1;1;1\n')

SLEEP = (
    'id;day;type;total_sleep_duration;average_hrv;lowest_heart_rate;average_breath\n'
    'long;2024-07-01;long_sleep;27000;52;51;14.5\n'
    'nap;2024-07-01;late_nap;1800;80;70;16\n'
    'gone;2024-07-01;deleted;40000;99;30;9\n'
    'nofigures;2024-07-02;long_sleep;25200;0;0;0\n')


def zip_of(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return buffer.getvalue()


def by_metric(samples):
    return {(s.metric_key, s.date): s.value for s in samples}


@pytest.mark.parametrize('name, kind', [
    ('dailyactivity.csv', 'activity'), ('App Data/DailyActivity.csv', 'activity'),
    ('dailyactivity (1).csv', 'activity'), ('daily_activity.csv', 'activity'), ('sleepmodel.csv', 'sleep'), ('sleep.csv', 'sleep_alt'), ('dailyspo2.csv', 'spo2'),
    ('vo2max.csv', 'vo2'), ('heartrate.csv', None), ('dailyactivity.json', None),
    ('__MACOSX/._dailyactivity.csv', None),
])
def test_file_kind(name, kind):
    assert file_kind(name) == kind


def test_activity_columns_become_daily_metrics():
    values = by_metric(parse_oura_export(ACTIVITY.encode(), 'dailyactivity.csv'))
    assert values == {
        ('steps', date(2024, 7, 1)): 8200.0,
        ('active_minutes', date(2024, 7, 1)): 40.0,       # (600 + 1800) s
        ('active_energy', date(2024, 7, 1)): 410.0,
    }


def test_sleep_adds_the_days_sessions_and_reads_the_longest_one():
    values = by_metric(parse_oura_export(zip_of({'App Data/sleepmodel.csv': SLEEP}), 'oura.zip'))
    # 27000 s + 1800 s of sleep; the deleted session is not sleep.
    assert values[('sleep_duration', date(2024, 7, 1))] == 8.0
    assert values[('hrv_rmssd', date(2024, 7, 1))] == 52.0
    assert values[('resting_hr', date(2024, 7, 1))] == 51.0
    assert values[('respiratory_rate', date(2024, 7, 1))] == 14.5
    # Oura writes 0 where it has no figure: the night is stored, the zeros are not.
    assert values[('sleep_duration', date(2024, 7, 2))] == 7.0
    assert ('hrv_rmssd', date(2024, 7, 2)) not in values
    assert ('resting_hr', date(2024, 7, 2)) not in values


def test_hrv_is_never_filed_as_sdnn():
    metrics = {s.metric_key for s in parse_oura_export(SLEEP.encode(), 'sleepmodel.csv')}
    assert 'hrv_sdnn' not in metrics and 'hrv_rmssd' in metrics


def test_spo2_reads_a_number_or_the_nested_average():
    plain = 'day,spo2_percentage\n2024-07-01,97.4\n'
    nested = "day;spo2_percentage\n2024-07-01;{'average': 96.5}\n"
    assert by_metric(parse_oura_export(plain.encode(), 'dailyspo2.csv')) == {
        ('spo2', date(2024, 7, 1)): 97.4}
    assert by_metric(parse_oura_export(nested.encode(), 'dailyspo2.csv')) == {
        ('spo2', date(2024, 7, 1)): 96.5}


def test_vo2_max_and_a_comma_separated_file_with_a_byte_order_mark():
    text = '﻿day,vo2_max\n2024-07-03,41.2\n'
    assert by_metric(parse_oura_export(text.encode(), 'vo2max.csv')) == {
        ('vo2_max', date(2024, 7, 3)): 41.2}


def test_a_column_that_is_not_there_is_skipped_not_guessed():
    samples = parse_oura_export(b'day;steps\n2024-07-01;100\n', 'dailyactivity.csv')
    assert {s.metric_key for s in samples} == {'steps'}


def test_files_the_parser_does_not_read_are_ignored_but_a_zip_of_nothing_is_refused():
    archive = zip_of({'App Data/dailyactivity.csv': ACTIVITY, 'App Data/heartrate.csv': 'x;y\n1;2\n'})
    assert parse_oura_export(archive, 'oura.zip')
    with pytest.raises(ValueError, match='No Oura export files'):
        parse_oura_export(zip_of({'notes.txt': 'hello'}), 'oura.zip')


def test_not_a_zip_is_refused():
    with pytest.raises(ValueError, match='not a valid ZIP'):
        parse_oura_export(b'plain text', 'oura.zip')


def test_an_oversized_member_is_refused_before_it_is_read(monkeypatch):
    monkeypatch.setattr(oura_parser, 'MAX_MEMBER_BYTES', 10)
    with pytest.raises(ValueError, match='too large'):
        parse_oura_export(zip_of({'dailyactivity.csv': ACTIVITY}), 'oura.zip')


def test_too_many_files_are_refused(monkeypatch):
    monkeypatch.setattr(oura_parser, 'MAX_MEMBERS', 1)
    with pytest.raises(ValueError, match='too many'):
        parse_oura_export(zip_of({'a.csv': 'x', 'b.csv': 'y'}), 'oura.zip')


def test_a_day_with_nothing_in_it_is_no_reading():
    text = 'day;steps;high_activity_time;medium_activity_time;active_calories\n2024-07-01;0;0;0;0\n2024-07-02;;;;\n'
    assert parse_oura_export(text.encode(), 'dailyactivity.csv') == []


def test_a_repeated_day_is_the_later_row_not_a_second_total():
    text = 'day;steps\n2024-07-01;100\n2024-07-01;250\n'
    assert by_metric(parse_oura_export(text.encode(), 'dailyactivity.csv')) == {
        ('steps', date(2024, 7, 1)): 250.0}


def test_a_plain_sleep_file_is_not_added_to_sleepmodel():
    archive = zip_of({'sleepmodel.csv': SLEEP, 'sleep.csv': SLEEP})
    values = by_metric(parse_oura_export(archive, 'oura.zip'))
    assert values[('sleep_duration', date(2024, 7, 1))] == 8.0
    alone = by_metric(parse_oura_export(zip_of({'sleep.csv': SLEEP}), 'oura.zip'))
    assert alone[('sleep_duration', date(2024, 7, 1))] == 8.0
