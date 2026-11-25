"""Each 30-day wearable metric can be traced to its rows and charted (#2887).

``GET /api/v1/patient-records/<id>/wearable-metric/?field=`` returns the daily
values behind one 30-day aggregate and, for the administrators who see the OMOP
tab, the concept groups holding the rows.
"""
from datetime import date, timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from omop_core.models import PatientRecord
from omop_core.services import wearable_metric_series as series
from omop_core.services.mappings import WEARABLE_CONCEPT_CODE, WEARABLE_METRIC_FIELDS
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory
from tests.test_omop_rows_by_concept import (  # noqa: F401  (world is a fixture)
    HOSPITAL_TYPE_ID, client_as, measurement, observation, world,
)

pytestmark = pytest.mark.django_db

DAY = date(2026, 11, 20)


def url(w, field):
    return f"/api/v1/patient-records/{w['person'].person_id}/wearable-metric/?field={field}"


def get(w, who, field):
    return client_as(w[who]).get(url(w, field))


def anchor(w, day=DAY):
    from django.utils import timezone
    PatientRecord.objects.filter(person=w['person']).update(
        wearable_last_sync_at=timezone.make_aware(
            timezone.datetime.combine(day, timezone.datetime.min.time())))


def test_every_30_day_field_has_a_metric():
    """A field the Wearables tab shows is a field the series can read."""
    declared = {name for entries in WEARABLE_METRIC_FIELDS.values() for name, _ in entries}
    assert set(series.FIELD_METRICS) == declared
    assert all(metric in WEARABLE_CONCEPT_CODE for metric, _ in series.FIELD_METRICS.values())


def test_steps_are_a_days_total_with_the_window_marked(world):
    w = world
    for offset, values in ((0, (3000, 2500)), (1, (4000,)), (45, (9000,))):
        for value in values:
            observation(w, w['steps'].concept_id, DAY - timedelta(days=offset), value=value,
                        type_id=HOSPITAL_TYPE_ID)
    anchor(w)
    body = get(w, 'staff', 'median_daily_steps_30d').json()
    assert body['statistic'] == 'sum'
    assert body['window'] == {'start': '2026-10-22', 'end': '2026-11-20'}
    days = {d['date']: d for d in body['days']}
    assert days['2026-11-20']['value'] == 5500 and days['2026-11-20']['rows'] == 2
    assert days['2026-11-19']['value'] == 4000
    assert days['2026-11-20']['in_window'] and days['2026-11-19']['in_window']
    assert days['2026-10-06']['in_window'] is False
    # oldest first, so the chart reads left to right
    assert [d['date'] for d in body['days']] == sorted(days)


def test_a_days_value_is_a_mean_for_a_rate_and_a_minimum_for_min_spo2(world):
    w = world
    rest = w['rest_hr']
    spo2 = ConceptFactory(concept_name='SpO2', concept_code=WEARABLE_CONCEPT_CODE['spo2'],
                          vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
                          domain=DomainFactory(domain_id='Measurement'))
    for value in (60, 70):
        measurement(w, rest.concept_id, DAY, value=value)
    for value in (96, 91, 94):
        measurement(w, spo2.concept_id, DAY, value=value)
    anchor(w)
    assert get(w, 'staff', 'resting_heart_rate_avg_30d').json()['days'][0]['value'] == 65
    mean = get(w, 'staff', 'oxygen_saturation_avg_30d').json()
    low = get(w, 'staff', 'oxygen_saturation_min_30d').json()
    assert mean['statistic'] == 'mean' and mean['days'][0]['value'] == 93.67
    assert low['statistic'] == 'min' and low['days'][0]['value'] == 91


def test_the_derivations_own_exclusions_apply(world):
    """Out-of-bounds readings, entered-in-error rows and other concepts are not behind the metric."""
    w = world
    measurement(w, w['rest_hr'].concept_id, DAY, value=62)
    measurement(w, w['rest_hr'].concept_id, DAY, value=900)                 # outside 20..300
    measurement(w, w['rest_hr'].concept_id, DAY, value=70, erroneous=True)  # entered in error
    measurement(w, w['glucose'].concept_id, DAY, value=100)                 # another concept
    anchor(w)
    day, = get(w, 'staff', 'resting_heart_rate_avg_30d').json()['days']
    assert (day['value'], day['rows']) == (62, 1)


def test_a_day_takes_its_value_from_accepted_rows_and_provisional_ones_stand_alone(world):
    """The plotted days reproduce the stored aggregate, which reads accepted rows only."""
    w = world
    rest = w['rest_hr'].concept_id
    measurement(w, rest, DAY, value=60)                                         # accepted only
    measurement(w, rest, DAY - timedelta(days=1), value=61, confidence=55)      # provisional only
    measurement(w, rest, DAY - timedelta(days=2), value=62, confidence=55)      # both: accepted wins
    measurement(w, rest, DAY - timedelta(days=2), value=64)
    anchor(w)
    days = {d['date']: d for d in get(w, 'staff', 'resting_heart_rate_avg_30d').json()['days']}
    assert (days['2026-11-20']['status'], days['2026-11-20']['value']) == ('accepted', 60)
    assert (days['2026-11-19']['status'], days['2026-11-19']['value']) == ('provisional', 61)
    both = days['2026-11-18']
    assert (both['status'], both['value'], both['rows']) == ('accepted', 64, 1)
    assert both['provisional_rows_excluded'] == 1 and days['2026-11-20']['provisional_rows_excluded'] == 0


def test_a_total_does_not_add_a_provisional_reading_to_an_accepted_one(world):
    w = world
    observation(w, w['steps'].concept_id, DAY, value=5000)
    observation(w, w['steps'].concept_id, DAY, value=3000, confidence=55)
    anchor(w)
    day, = get(w, 'staff', 'median_daily_steps_30d').json()['days']
    assert day['value'] == 5000


def test_rows_in_both_tables_are_one_days_value(world):
    """A metric's rows may sit in measurements and in observations; the day is one."""
    w = world
    measurement(w, w['steps'].concept_id, DAY, value=1000, source='steps')
    observation(w, w['steps'].concept_id, DAY, value=2500)
    anchor(w)
    body = get(w, 'staff', 'median_daily_steps_30d').json()
    day, = body['days']
    assert (day['value'], day['rows']) == (3500, 2)
    assert {g['table'] for g in body['groups']} == {'measurements', 'observations'}


def test_the_series_keeps_the_newest_365_days(world):
    w = world
    for offset in range(370):
        measurement(w, w['rest_hr'].concept_id, DAY - timedelta(days=offset), value=60)
    anchor(w)
    body = get(w, 'staff', 'resting_heart_rate_avg_30d').json()
    assert body['truncated'] is True and len(body['days']) == 365
    assert body['days'][-1]['date'] == DAY.isoformat()


def test_a_devices_unresolved_rows_and_a_hospitals_of_one_spelling_are_two_groups(world):
    """As the OMOP tab lists them, so the link opens the group it names."""
    from omop_core.models import PatientSourceCode
    from omop_core.services.mappings import WEARABLE_TYPE_CONCEPT_ID
    w = world
    code = WEARABLE_CONCEPT_CODE['resting_hr']
    PatientSourceCode.objects.create(person=w['person'], source_vocabulary_id='Garmin',
                                     omop_table='measurement', source_value=code)
    measurement(w, 0, DAY, value=61, source=code, type_id=WEARABLE_TYPE_CONCEPT_ID)
    measurement(w, 0, DAY - timedelta(days=1), value=62, source=code)   # a hospital's
    anchor(w)
    groups = get(w, 'org_admin', 'resting_heart_rate_avg_30d').json()['groups']
    unresolved = sorted((g['wearable'], g['rows']) for g in groups if g['concept_id'] == 0)
    assert unresolved == [(False, 1), (True, 1)]


def test_a_row_whose_concept_is_unrelated_is_found_by_its_wearable_source_value(world):
    """The derivation falls back to the source value when the concept is not a wearable code."""
    w = world
    measurement(w, w['glucose'].concept_id, DAY, value=66, source=WEARABLE_CONCEPT_CODE['resting_hr'])
    anchor(w)
    day, = get(w, 'staff', 'resting_heart_rate_avg_30d').json()['days']
    assert day['value'] == 66


def test_groups_name_where_the_rows_are_for_an_administrator_only(world):
    w = world
    observation(w, w['steps'].concept_id, DAY, value=100)
    observation(w, w['steps'].concept_id, DAY - timedelta(days=1), value=200)
    anchor(w)
    groups = get(w, 'org_admin', 'median_daily_steps_30d').json()['groups']
    assert len(groups) == 1
    assert groups[0]['table'] == 'observations'
    assert groups[0]['concept_id'] == w['steps'].concept_id
    assert groups[0]['rows'] == 2 and groups[0]['concept_name'] == 'Steps'
    assert groups[0]['source_value'] is None
    # The patient reads their own chart and is shown no row-level link.
    own = get(w, 'patient', 'median_daily_steps_30d')
    assert own.status_code == 200
    assert own.json()['groups'] is None and len(own.json()['days']) == 2


def test_no_rows_is_an_empty_series_not_an_error(world):
    body = get(world, 'staff', 'median_daily_steps_30d').json()
    assert body['days'] == [] and body['window'] is None and body['groups'] == []


def test_without_a_stored_sync_the_window_ends_on_the_newest_day(world):
    w = world
    measurement(w, w['rest_hr'].concept_id, DAY, value=60)
    body = get(w, 'staff', 'resting_heart_rate_avg_30d').json()
    assert body['window']['end'] == DAY.isoformat()


def test_who_may_read_it(world):
    w = world
    field = 'median_daily_steps_30d'
    assert get(w, 'doctor', field).status_code == 200
    assert get(w, 'outsider', field).status_code == 404
    assert get(w, 'analyst', field).status_code == 404   # an analyst reads no patient
    from rest_framework.test import APIClient
    assert APIClient().get(url(w, field)).status_code in (401, 403)


def test_a_field_that_is_not_a_30_day_metric_is_refused(world):
    assert get(world, 'staff', 'date_of_birth').status_code == 400
    assert get(world, 'staff', '').status_code == 400


def test_the_query_count_does_not_grow_with_the_rows(world):
    w = world

    def count():
        with CaptureQueriesContext(connection) as queries:
            assert get(w, 'staff', 'resting_heart_rate_avg_30d').status_code == 200
        return len(queries)

    measurement(w, w['rest_hr'].concept_id, DAY, value=60)
    few = count()
    for offset in range(1, 25):
        measurement(w, w['rest_hr'].concept_id, DAY - timedelta(days=offset), value=60 + offset)
    assert count() == few
