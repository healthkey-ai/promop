"""The OMOP tab groups measurements and observations by concept, and the Source
Codes tab opens a wearable code to its readings (#2863).

Three reads, all admin-only and all v1-only: ``omop/`` (the groups),
``omop-rows/`` (one group's rows, newest first, paged) and
``source-code-rows/`` (one wearable code's rows). The legacy
``/api/patient-info/<id>/omop/`` keeps its raw dump.
"""
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext, override_settings
from rest_framework.test import APIClient

from omop_core.models import (
    ConditionOccurrence, DrugExposure, GroupAccess, Measurement, Observation, ProcedureOccurrence,
    PatientSourceCode,
)
from omop_core.services import omop_row_pages
from omop_core.services.mappings import WEARABLE_TYPE_CONCEPT_ID
from patient_portal.models import Identity, PatientUser
from tests.factories import (
    ConceptFactory, DomainFactory, OrganizationFactory, PatientRecordFactory, PersonFactory,
    VocabularyFactory,
)

pytestmark = pytest.mark.django_db

HOSPITAL_TYPE_ID = 44818702


def utc(*args):
    return datetime(*args, tzinfo=dt_timezone.utc)


@pytest.fixture
def world():
    """A patient in one organization, the concepts and the identities that may or may not read her."""
    org = OrganizationFactory(slug='omop-rows-org')
    person = PersonFactory()
    PatientRecordFactory(person=person, organization=org)
    ConceptFactory(concept_id=0, concept_name='No matching concept', concept_code='0',
                   vocabulary=VocabularyFactory(vocabulary_id='None'), standard_concept=None)
    wearable_type = ConceptFactory(concept_id=WEARABLE_TYPE_CONCEPT_ID, concept_code='PSR',
                                   concept_name='Patient self-report')
    hospital_type = ConceptFactory(concept_id=HOSPITAL_TYPE_ID, concept_code='LAB',
                                   concept_name='Lab result')
    loinc = VocabularyFactory(vocabulary_id='LOINC')
    steps = ConceptFactory(concept_name='Steps', concept_code='55423-8', vocabulary=loinc,
                           domain=DomainFactory(domain_id='Observation'))
    glucose = ConceptFactory(concept_name='Glucose', concept_code='2345-7', vocabulary=loinc)
    rest_hr = ConceptFactory(concept_name='Resting heart rate', concept_code='40443-4', vocabulary=loinc)

    def identity(email, **more):
        return Identity.objects.create_user(email=email, password='pw', **more)

    staff = identity('staff@omop-rows.test', is_staff=True)
    org_admin = identity('admin@omop-rows.test')
    GroupAccess.objects.create(identity=org_admin, org=org, role='org_admin')
    doctor = identity('doctor@omop-rows.test')
    GroupAccess.objects.create(identity=doctor, org=org, role='doctor')
    analyst = identity('analyst@omop-rows.test')
    GroupAccess.objects.create(identity=analyst, org=org, role='analyst')
    patient = identity('patient@omop-rows.test')
    PatientUser.objects.create(identity=patient, person=person)
    other_org = OrganizationFactory(slug='omop-rows-other-org')
    outsider = identity('outsider@omop-rows.test')
    GroupAccess.objects.create(identity=outsider, org=other_org, role='org_admin')

    return dict(org=org, person=person, wearable_type=wearable_type, hospital_type=hospital_type,
                steps=steps, glucose=glucose, rest_hr=rest_hr, staff=staff, org_admin=org_admin,
                doctor=doctor, analyst=analyst, patient=patient, outsider=outsider)


def client_as(identity, token=None):
    client = APIClient()
    client.force_authenticate(user=identity, token=token)
    return client


_ids = iter(range(70_000_000, 80_000_000))


def measurement(w, concept, day, *, when=None, value=1, source='x', type_id=None, confidence=None,
                person=None, erroneous=False, unit='mg/dL'):
    return Measurement.objects.create(
        measurement_id=next(_ids), person=person or w['person'], measurement_concept_id=concept,
        measurement_date=day, measurement_datetime=when,
        measurement_type_concept_id=type_id or HOSPITAL_TYPE_ID, value_as_number=value,
        measurement_source_value=source, unit_source_value=unit, confidence=confidence,
        is_erroneous=erroneous)


def observation(w, concept, day, *, when=None, value=1, source='steps', type_id=None,
                confidence=None, person=None, erroneous=False):
    return Observation.objects.create(
        observation_id=next(_ids), person=person or w['person'], observation_concept_id=concept,
        observation_date=day, observation_datetime=when,
        observation_type_concept_id=type_id or HOSPITAL_TYPE_ID, value_as_number=value,
        observation_source_value=source, unit_source_value='/d', confidence=confidence,
        is_erroneous=erroneous)


def omop_url(w, name='omop'):
    return f"/api/v1/patient-records/{w['person'].person_id}/{name}/"


def table(resp, key):
    return next(t for t in resp.data['tables'] if t['key'] == key)


# ── The OMOP tab's groups ────────────────────────────────────────────────────────────


def test_measurements_come_back_as_one_group_per_concept(world):
    w = world
    g, hr = w['glucose'].concept_id, w['rest_hr'].concept_id
    measurement(w, g, date(2024, 1, 1), when=utc(2024, 1, 1, 8, 30), source='GLU')
    measurement(w, g, date(2024, 3, 5), when=utc(2024, 3, 5, 9, 15), source='GLU', confidence=70)
    measurement(w, g, date(2024, 2, 1), source='GLU')                       # a day only
    measurement(w, hr, date(2024, 4, 1), source='rhr', type_id=WEARABLE_TYPE_CONCEPT_ID)
    ConditionOccurrence.objects.create(
        condition_occurrence_id=next(_ids), person=w['person'], condition_concept_id=0,
        condition_type_concept_id=0, condition_start_date=date(2024, 1, 15), condition_source_value='c')

    resp = client_as(w['staff']).get(omop_url(w))

    assert resp.status_code == 200
    measurements = table(resp, 'measurements')
    assert measurements['grouped'] is True
    assert measurements['count'] == 4
    assert measurements['rows'] == []
    assert measurements['truncated'] is False
    assert measurements['unresolved_groups'] == []
    first, second = measurements['groups']       # latest day first
    assert (first['concept_id'], first['concept_name'], first['concept_code'], first['vocabulary_id']) == (
        hr, 'Resting heart rate', '40443-4', 'LOINC')
    assert first['count'] == 1 and first['rows_with_time'] == 0
    assert (first['latest_date'], first['latest_datetime']) == ('2024-04-01', None)
    assert second['concept_name'] == 'Glucose' and second['count'] == 3
    assert second['provisional_rows'] == 1 and second['rows_with_time'] == 2
    # The latest moment is the 5 March reading; the earliest is the 1 January one.
    assert second['latest_date'] == '2024-03-05'
    assert second['latest_datetime'] == '2024-03-05T09:15:00+00:00'
    assert second['earliest_date'] == '2024-01-01'
    assert second['earliest_datetime'] == '2024-01-01T08:30:00+00:00'
    # Other tables are the raw rows they were.
    conditions = table(resp, 'condition_occurrences')
    assert conditions.get('grouped') is None and conditions['count'] == 1
    assert conditions['rows'][0]['condition_source_value'] == 'c'


def test_a_day_only_row_later_than_the_last_datetime_is_what_the_group_shows(world):
    w = world
    g = w['glucose'].concept_id
    measurement(w, g, date(2024, 1, 1), when=utc(2024, 1, 1, 8, 0))
    measurement(w, g, date(2024, 2, 1))
    group = table(client_as(w['staff']).get(omop_url(w)), 'measurements')['groups'][0]
    assert (group['latest_date'], group['latest_datetime']) == ('2024-02-01', None)
    assert (group['earliest_date'], group['earliest_datetime']) == ('2024-01-01', '2024-01-01T08:00:00+00:00')


def test_rows_at_concept_zero_are_grouped_by_source_value_apart_from_the_concepts(world):
    w = world
    measurement(w, 0, date(2024, 1, 1), source='spo2', type_id=WEARABLE_TYPE_CONCEPT_ID)
    measurement(w, 0, date(2024, 1, 2), source='spo2', type_id=WEARABLE_TYPE_CONCEPT_ID)
    measurement(w, 0, date(2024, 1, 3), source='LOCAL-9')
    measurement(w, 0, date(2024, 1, 4), source=None)
    measurement(w, w['glucose'].concept_id, date(2024, 1, 5))

    measurements = table(client_as(w['staff']).get(omop_url(w)), 'measurements')

    assert [g['concept_name'] for g in measurements['groups']] == ['Glucose']
    unresolved = {g['source_value']: g['count'] for g in measurements['unresolved_groups']}
    assert unresolved == {'spo2': 2, 'LOCAL-9': 1, '': 1}
    assert all(g['concept_id'] == 0 for g in measurements['unresolved_groups'])
    assert measurements['count'] == 5


def hold(w, code, vocabulary, table):
    PatientSourceCode.objects.create(
        person=w['person'], source_value=code, source_vocabulary_id=vocabulary,
        omop_table=table, occurrence_count=1)


def test_a_group_of_a_devices_rows_is_marked_a_wearable_and_a_hospitals_is_not(world):
    w = world
    hold(w, 'rhr', 'Garmin', 'measurement')
    measurement(w, w['rest_hr'].concept_id, date(2024, 1, 2), source='rhr', type_id=WEARABLE_TYPE_CONCEPT_ID)
    measurement(w, w['glucose'].concept_id, date(2024, 1, 1), source='GLU')
    # A hospital's row filed under the spelling a device uses: not a device's row.
    measurement(w, w['glucose'].concept_id, date(2024, 1, 3), source='rhr')

    groups = {g['concept_name']: g for g in table(client_as(w['staff']).get(omop_url(w)), 'measurements')['groups']}

    assert groups['Resting heart rate']['wearable'] is True
    assert groups['Resting heart rate']['wearable_rows'] == 1
    assert groups['Glucose']['wearable'] is False and groups['Glucose']['wearable_rows'] == 0


def test_a_row_of_the_importers_type_is_no_wearable_while_the_inventory_does_not_hold_its_code(world):
    w = world
    measurement(w, w['rest_hr'].concept_id, date(2024, 1, 2), source='rhr', type_id=WEARABLE_TYPE_CONCEPT_ID)
    hold(w, 'rhr', 'HT-Epic', 'measurement')                  # a hospital's inventory entry
    hold(w, 'other', 'Garmin', 'measurement')                  # a device's, another code
    group = table(client_as(w['staff']).get(omop_url(w)), 'measurements')['groups'][0]
    assert group['wearable'] is False


def test_a_device_and_a_hospital_using_one_spelling_are_two_unresolved_groups(world):
    w = world
    hold(w, 'spo2', 'Garmin', 'measurement')
    device = measurement(w, 0, date(2024, 1, 2), source='spo2', value=97, type_id=WEARABLE_TYPE_CONCEPT_ID)
    local = measurement(w, 0, date(2024, 1, 3), source='spo2', value=88)

    unresolved = table(client_as(w['staff']).get(omop_url(w)), 'measurements')['unresolved_groups']
    by_mark = {g['wearable']: g for g in unresolved}

    assert sorted(by_mark) == [False, True] and len(unresolved) == 2
    assert by_mark[True]['count'] == 1 and by_mark[True]['source_value'] == 'spo2'
    assert by_mark[False]['count'] == 1 and by_mark[False]['source_value'] == 'spo2'
    # Each group reads its own rows.
    only_device = rows_of(w, concept=0, source_value='spo2', wearable='true')
    only_local = rows_of(w, concept=0, source_value='spo2', wearable='false')
    both = rows_of(w, concept=0, source_value='spo2')
    assert [r['id'] for r in only_device.data['rows']] == [device.pk]
    assert [r['id'] for r in only_local.data['rows']] == [local.pk]
    assert {r['id'] for r in both.data['rows']} == {device.pk, local.pk}
    assert rows_of(w, concept=0, source_value='spo2', wearable='maybe').status_code == 400


def test_with_no_device_codes_held_no_row_is_a_devices(world):
    w = world
    measurement(w, 0, date(2024, 1, 2), source='spo2', type_id=WEARABLE_TYPE_CONCEPT_ID)
    assert rows_of(w, concept=0, source_value='spo2', wearable='true').data['rows'] == []
    assert len(rows_of(w, concept=0, source_value='spo2', wearable='false').data['rows']) == 1


def test_observation_groups_are_marked_too(world):
    w = world
    hold(w, 'steps', 'Garmin', 'observation')
    observation(w, w['steps'].concept_id, date(2024, 1, 1), source='steps', type_id=WEARABLE_TYPE_CONCEPT_ID)
    observation(w, w['glucose'].concept_id, date(2024, 1, 3), source='steps')       # a hospital's
    observation(w, 0, date(2024, 1, 2), source='steps', type_id=WEARABLE_TYPE_CONCEPT_ID)
    observations = table(client_as(w['staff']).get(omop_url(w)), 'observations')
    assert {g['concept_name']: g['wearable'] for g in observations['groups']} == {
        'Steps': True, 'Glucose': False}
    assert [g['wearable'] for g in observations['unresolved_groups']] == [True]


def test_observations_are_grouped_the_same_way(world):
    w = world
    observation(w, w['steps'].concept_id, date(2024, 1, 1))
    observation(w, w['steps'].concept_id, date(2024, 1, 2))
    observation(w, 0, date(2024, 1, 3), source='flights')
    observations = table(client_as(w['staff']).get(omop_url(w)), 'observations')
    assert observations['grouped'] is True and observations['count'] == 3
    assert [(g['concept_name'], g['count']) for g in observations['groups']] == [('Steps', 2)]
    assert [(g['source_value'], g['count']) for g in observations['unresolved_groups']] == [('flights', 1)]


def test_another_patients_rows_are_not_in_the_groups(world):
    w = world
    stranger = PersonFactory()
    measurement(w, w['glucose'].concept_id, date(2024, 1, 1))
    measurement(w, w['glucose'].concept_id, date(2024, 1, 2), person=stranger)
    group = table(client_as(w['staff']).get(omop_url(w)), 'measurements')['groups'][0]
    assert group['count'] == 1


def test_too_many_groups_are_cut_and_say_so(world, monkeypatch):
    w = world
    monkeypatch.setattr(omop_row_pages, 'MAX_GROUPS', 2)
    for i in range(3):
        measurement(w, 0, date(2024, 1, 1 + i), source=f'code-{i}')
    measurements = table(client_as(w['staff']).get(omop_url(w)), 'measurements')
    assert measurements['truncated'] is True
    assert len(measurements['unresolved_groups']) == 2
    assert measurements['count'] == 3
    # The most recent first, so what is cut is the oldest.
    assert [g['source_value'] for g in measurements['unresolved_groups']] == ['code-2', 'code-1']


def test_the_legacy_registration_keeps_the_raw_dump(world):
    w = world
    measurement(w, w['glucose'].concept_id, date(2024, 1, 1))
    resp = client_as(w['staff']).get(f"/api/patient-info/{w['person'].person_id}/omop/")
    assert resp.status_code == 200
    measurements = table(resp, 'measurements')
    assert 'grouped' not in measurements
    assert len(measurements['rows']) == 1


# ── One group's rows ─────────────────────────────────────────────────────────────────


def rows_of(w, *, table_key='measurements', concept=None, who=None, **params):
    query = {'table': table_key, 'concept': w['glucose'].concept_id if concept is None else concept,
             **params}
    return (client_as(who or w['staff']).get(omop_url(w, 'omop-rows'), query))


def test_rows_are_newest_first_by_datetime_then_date_then_id(world):
    w = world
    g = w['glucose'].concept_id
    a = measurement(w, g, date(2024, 1, 1), when=utc(2024, 1, 1, 8, 0), value=1)
    b = measurement(w, g, date(2024, 1, 1), when=utc(2024, 1, 1, 17, 0), value=2)
    c = measurement(w, g, date(2024, 1, 1), value=3)                       # a day only
    d = measurement(w, g, date(2024, 1, 1), value=4)                       # a day only, later id
    e = measurement(w, g, date(2024, 1, 2), value=5)
    f = measurement(w, g, date(2023, 12, 31), when=utc(2023, 12, 31, 23, 0), value=6)
    tie_1 = measurement(w, g, date(2024, 1, 1), when=utc(2024, 1, 1, 17, 0), value=7)

    resp = rows_of(w)

    assert resp.status_code == 200
    assert [r['id'] for r in resp.data['rows']] == [
        e.pk,                    # a later day
        tie_1.pk, b.pk,          # the same moment: the larger id first
        a.pk,                    # an earlier moment that day
        d.pk, c.pk,              # day-only rows after the day's datetimes, larger id first
        f.pk]
    assert resp.data['next_cursor'] is None
    first = resp.data['rows'][0]
    assert first == {
        'id': e.pk, 'date': '2024-01-02', 'datetime': None, 'value_as_number': 5.0,
        'value_as_string': None, 'unit': 'mg/dL', 'source_value': 'x', 'concept_id': g,
        'status': 'accepted', 'confidence': None, 'is_erroneous': False}
    assert resp.data['rows'][1]['datetime'] == '2024-01-01T17:00:00+00:00'


def test_a_group_is_read_in_pages_without_repeats_or_gaps(world):
    w = world
    g = w['glucose'].concept_id
    made = []
    for day in range(1, 8):
        made.append(measurement(w, g, date(2024, 1, day), when=utc(2024, 1, day, 8, 0)))
        made.append(measurement(w, g, date(2024, 1, day)))
        made.append(measurement(w, g, date(2024, 1, day), when=utc(2024, 1, day, 8, 0)))
    expected = [r['id'] for r in rows_of(w, limit=200).data['rows']]
    assert len(expected) == len(made) == 21

    seen, cursor, pages = [], None, 0
    while True:
        params = {'limit': 4, **({'cursor': cursor} if cursor else {})}
        resp = rows_of(w, **params)
        assert resp.status_code == 200
        assert len(resp.data['rows']) <= 4
        seen += [r['id'] for r in resp.data['rows']]
        pages += 1
        cursor = resp.data['next_cursor']
        if cursor is None:
            break
    assert seen == expected
    assert pages == 6


def test_status_is_accepted_provisional_or_unresolved(world):
    w = world
    g = w['glucose'].concept_id
    measurement(w, g, date(2024, 1, 3))
    measurement(w, g, date(2024, 1, 2), confidence=70)
    measurement(w, 0, date(2024, 1, 1), source='LOCAL-1')
    assert [(r['status'], r['confidence']) for r in rows_of(w).data['rows']] == [
        ('accepted', None), ('provisional', 70)]
    resp = rows_of(w, concept=0, source_value='LOCAL-1')
    assert [(r['status'], r['concept_id']) for r in resp.data['rows']] == [('unresolved', 0)]


def test_unresolved_rows_are_read_by_source_value_and_the_blank_one_by_an_empty_value(world):
    w = world
    measurement(w, 0, date(2024, 1, 1), source='A', value=1)
    measurement(w, 0, date(2024, 1, 2), source='B', value=2)
    measurement(w, 0, date(2024, 1, 3), source=None, value=3)
    measurement(w, 0, date(2024, 1, 4), source='', value=4)
    assert [r['value_as_number'] for r in rows_of(w, concept=0, source_value='B').data['rows']] == [2.0]
    assert [r['value_as_number'] for r in rows_of(w, concept=0, source_value='').data['rows']] == [4.0, 3.0]
    assert rows_of(w, concept=0).status_code == 400
    # A concept's rows are never mixed with the unresolved ones.
    measurement(w, w['glucose'].concept_id, date(2024, 1, 5), source='A')
    assert len(rows_of(w, concept=0, source_value='A').data['rows']) == 1


def test_observation_rows_and_a_unit_from_the_concept(world):
    w = world
    ucum = ConceptFactory(concept_name='milligram per deciliter', concept_code='mg/dL',
                          vocabulary=VocabularyFactory(vocabulary_id='UCUM'))
    row = Measurement.objects.create(
        measurement_id=next(_ids), person=w['person'], measurement_concept_id=w['glucose'].concept_id,
        measurement_date=date(2024, 1, 1), measurement_type_concept_id=HOSPITAL_TYPE_ID,
        value_as_number=Decimal('5.50000'), unit_concept=ucum)
    assert rows_of(w).data['rows'][0] == {
        **rows_of(w).data['rows'][0], 'id': row.pk, 'unit': 'mg/dL', 'value_as_number': 5.5,
        'source_value': None}
    observation(w, w['steps'].concept_id, date(2024, 1, 1), value=9000)
    resp = rows_of(w, table_key='observations', concept=w['steps'].concept_id)
    assert [(r['value_as_number'], r['unit']) for r in resp.data['rows']] == [(9000.0, '/d')]


def test_rows_of_an_entered_in_error_row_are_flagged_not_hidden(world):
    w = world
    measurement(w, w['glucose'].concept_id, date(2024, 1, 1), erroneous=True)
    assert [r['is_erroneous'] for r in rows_of(w).data['rows']] == [True]


@pytest.mark.parametrize('params', [
    {'table': 'conditions', 'concept': 1}, {'table': '', 'concept': 1},
    {'table': 'measurements', 'concept': 'abc'}, {'table': 'measurements'},
    {'table': 'measurements', 'concept': 1, 'cursor': 'not-a-cursor'},
    {'table': 'measurements', 'concept': 1, 'limit': 0},
    {'table': 'measurements', 'concept': 1, 'limit': 201},
    {'table': 'measurements', 'concept': 1, 'limit': 'many'},
])
def test_a_bad_request_is_a_400(world, params):
    resp = client_as(world['staff']).get(omop_url(world, 'omop-rows'), params)
    assert resp.status_code == 400


@pytest.mark.parametrize('text', [
    'e30', 'W10', 'WyIyMDI0LTAxLTAxIiwgbnVsbCwgInguIl0',
    'WyIyMDI0LTAxLTAxIiwgIjIwMjQtMDEtMDFUMDg6MDA6MDAiLCAxXQ',     # a naive datetime
])
def test_a_cursor_this_module_did_not_issue_is_refused(text):
    with pytest.raises(ValueError):
        omop_row_pages.decode_cursor(text)


def test_a_cursor_round_trips():
    moment = utc(2024, 1, 2, 3, 4, 5)
    assert omop_row_pages.decode_cursor(omop_row_pages.encode_cursor(date(2024, 1, 2), moment, 7)) == (
        date(2024, 1, 2), moment, 7)
    assert omop_row_pages.decode_cursor(omop_row_pages.encode_cursor(date(2024, 1, 2), None, 7)) == (
        date(2024, 1, 2), None, 7)


# ── Who may read ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def held_wearable_code(world):
    w = world
    PatientSourceCode.objects.create(
        person=w['person'], source_value='steps', source_vocabulary_id='Garmin',
        omop_table='observation', occurrence_count=1)
    observation(w, 0, date(2024, 1, 1), source='steps', type_id=WEARABLE_TYPE_CONCEPT_ID)
    return w


def all_three(w, who, token=None):
    client = client_as(who, token)
    return (
        client.get(omop_url(w)).status_code,
        client.get(omop_url(w, 'omop-rows'), {'table': 'measurements', 'concept': 0, 'source_value': ''}).status_code,
        client.get(omop_url(w, 'source-code-rows'),
                   {'table': 'observation', 'code': 'steps', 'vocabulary': 'Garmin'}).status_code,
    )


@pytest.mark.parametrize('who,expected', [
    ('staff', 200), ('org_admin', 200),
    ('doctor', 403),        # reads the record, is no administrator
    ('analyst', 404),       # no individual patient's data
    ('patient', 403),       # her own record, but not the raw rows
    ('outsider', 404),      # an administrator elsewhere
])
def test_who_reads_what(held_wearable_code, who, expected):
    assert all_three(held_wearable_code, held_wearable_code[who]) == (expected,) * 3


def test_no_one_signed_in_reads_nothing(held_wearable_code):
    w = held_wearable_code
    client = APIClient()
    for name, params in (('omop', {}), ('omop-rows', {'table': 'measurements', 'concept': 0}),
                         ('source-code-rows', {'table': 'observation', 'code': 'steps',
                                               'vocabulary': 'Garmin'})):
        assert client.get(omop_url(w, name), params).status_code in (401, 403)


@override_settings(SERVICE_AUTH_SCOPES='patient/*.read')
def test_a_service_token_reads_as_it_does_the_source_codes_list(held_wearable_code):
    w = held_wearable_code
    token = 'service-token'
    assert all_three(w, w['org_admin'], token) == (200, 200, 200)
    assert client_as(w['org_admin'], token).get(omop_url(w, 'source-codes')).status_code == 200


@override_settings(SERVICE_AUTH_SCOPES='')
def test_a_service_token_with_no_read_scope_reads_nothing(held_wearable_code):
    w = held_wearable_code
    assert all_three(w, w['org_admin'], 'service-token') == (403, 403, 403)


def test_the_endpoints_are_not_on_the_legacy_registration():
    """The legacy prefix registers PatientRecordViewSet itself: it must not gain the new actions."""
    from patient_portal.api.views import PatientRecordV1ViewSet, PatientRecordViewSet
    for name in ('omop_rows', 'source_code_rows'):
        assert not hasattr(PatientRecordViewSet, name)
        assert hasattr(PatientRecordV1ViewSet, name)
    assert PatientRecordViewSet.omop_grouped is False and PatientRecordV1ViewSet.omop_grouped is True


# ── A wearable code's readings ───────────────────────────────────────────────────────


def code_rows(w, who=None, **params):
    query = {'table': 'observation', 'code': 'steps', 'vocabulary': 'Garmin', **params}
    return client_as(who or w['staff']).get(omop_url(w, 'source-code-rows'), query)


def test_a_wearable_code_lists_its_readings_newest_first_with_their_status(world):
    w = world
    PatientSourceCode.objects.create(
        person=w['person'], source_value='steps', source_vocabulary_id='Garmin',
        omop_table='observation', occurrence_count=3)
    steps = w['steps'].concept_id
    observation(w, steps, date(2024, 1, 1), value=100, type_id=WEARABLE_TYPE_CONCEPT_ID)
    observation(w, steps, date(2024, 1, 3), value=300, type_id=WEARABLE_TYPE_CONCEPT_ID, confidence=70)
    observation(w, 0, date(2024, 1, 2), value=200, type_id=WEARABLE_TYPE_CONCEPT_ID)

    resp = code_rows(w)

    assert resp.status_code == 200
    assert [(r['date'], r['datetime'], r['value_as_number'], r['status'])
            for r in resp.data['rows']] == [
        ('2024-01-03', None, 300.0, 'provisional'),
        ('2024-01-02', None, 200.0, 'unresolved'),
        ('2024-01-01', None, 100.0, 'accepted')]
    assert resp.data['next_cursor'] is None


def test_a_devices_code_lists_only_the_importers_rows_under_exactly_that_code(world):
    w = world
    hold(w, 'spo2', 'Garmin', 'measurement')
    hold(w, 'spo2', 'HT-Epic', 'measurement')
    mine = measurement(w, 0, date(2024, 1, 2), source='spo2', value=97, type_id=WEARABLE_TYPE_CONCEPT_ID)
    measurement(w, 0, date(2024, 1, 3), source='spo2', value=88)                       # a hospital's
    flagged = measurement(w, 0, date(2024, 1, 4), source='spo2', value=99, type_id=WEARABLE_TYPE_CONCEPT_ID,
                          erroneous=True)
    measurement(w, 0, date(2024, 1, 5), source='SPO2', value=96, type_id=WEARABLE_TYPE_CONCEPT_ID)

    resp = code_rows(w, table='measurement', code='spo2')

    # Entered in error is shown, marked, not hidden.
    assert [(r['id'], r['is_erroneous']) for r in resp.data['rows']] == [(flagged.pk, True), (mine.pk, False)]


def test_a_code_the_patient_does_not_hold_is_a_404(world):
    w = world
    observation(w, 0, date(2024, 1, 1), source='steps', type_id=WEARABLE_TYPE_CONCEPT_ID)
    # Rows exist, but the inventory does not say the patient holds the code.
    assert code_rows(w).status_code == 404
    PatientSourceCode.objects.create(
        person=w['person'], source_value='steps', source_vocabulary_id='Garmin',
        omop_table='observation', occurrence_count=1)
    assert code_rows(w).status_code == 200
    assert code_rows(w, vocabulary='Apple').status_code == 404
    assert code_rows(w, table='measurement').status_code == 404
    assert code_rows(w, code='Steps').status_code == 404


def test_another_patient_holding_the_code_does_not_open_this_ones_rows(world):
    w = world
    stranger = PersonFactory()
    PatientSourceCode.objects.create(
        person=stranger, source_value='steps', source_vocabulary_id='Garmin',
        omop_table='observation', occurrence_count=1)
    observation(w, 0, date(2024, 1, 1), person=stranger, source='steps', type_id=WEARABLE_TYPE_CONCEPT_ID)
    assert code_rows(w).status_code == 404


def test_a_code_is_read_in_pages(world):
    w = world
    PatientSourceCode.objects.create(
        person=w['person'], source_value='steps', source_vocabulary_id='Garmin',
        omop_table='observation', occurrence_count=5)
    for day in range(1, 6):
        observation(w, 0, date(2024, 1, day), value=day, type_id=WEARABLE_TYPE_CONCEPT_ID)
    first = code_rows(w, limit=2)
    second = code_rows(w, limit=2, cursor=first.data['next_cursor'])
    third = code_rows(w, limit=2, cursor=second.data['next_cursor'])
    assert [r['value_as_number'] for r in first.data['rows']] == [5.0, 4.0]
    assert [r['value_as_number'] for r in second.data['rows']] == [3.0, 2.0]
    assert [r['value_as_number'] for r in third.data['rows']] == [1.0]
    assert third.data['next_cursor'] is None


def test_a_code_cut_to_the_column_width_matches_its_rows(world):
    w = world
    long_code = 'HKQuantityTypeIdentifier' + 'X' * 40               # 64 characters; the column holds 50
    PatientSourceCode.objects.create(
        person=w['person'], source_value=long_code, source_vocabulary_id='Apple',
        omop_table='observation', occurrence_count=1)
    observation(w, 0, date(2024, 1, 1), source=long_code[:50], type_id=WEARABLE_TYPE_CONCEPT_ID)
    resp = code_rows(w, code=long_code, vocabulary='Apple')
    assert resp.status_code == 200 and len(resp.data['rows']) == 1


@pytest.mark.parametrize('params', [
    {'table': 'conditions'}, {'table': ''}, {'code': ''}, {'limit': 0}, {'cursor': 'junk'},
])
def test_a_bad_code_request_is_a_400(held_wearable_code, params):
    assert code_rows(held_wearable_code, **params).status_code == 400


# ── Any source code's rows ───────────────────────────────────────────────────────────


def condition(w, day, *, when=None, source='C50.9', confidence=None, erroneous=False, concept=0, person=None):
    return ConditionOccurrence.objects.create(
        condition_occurrence_id=next(_ids), person=person or w['person'], condition_concept_id=concept,
        condition_type_concept_id=HOSPITAL_TYPE_ID, condition_start_date=day,
        condition_start_datetime=when, condition_source_value=source, confidence=confidence,
        is_erroneous=erroneous)


def drug(w, day, *, source='RX-1', quantity=None, unit=None, person=None):
    return DrugExposure.objects.create(
        drug_exposure_id=next(_ids), person=person or w['person'], drug_concept_id=0,
        drug_type_concept_id=HOSPITAL_TYPE_ID, drug_exposure_start_date=day,
        drug_exposure_end_date=day, drug_source_value=source, quantity=quantity,
        dose_unit_source_value=unit)


def procedure(w, day, *, source='PX-1', quantity=None, person=None):
    return ProcedureOccurrence.objects.create(
        procedure_occurrence_id=next(_ids), person=person or w['person'], procedure_concept_id=0,
        procedure_type_concept_id=HOSPITAL_TYPE_ID, procedure_date=day, procedure_source_value=source,
        quantity=quantity)


def any_code_rows(w, table_key, code, vocabulary='', who=None, **params):
    return client_as(who or w['staff']).get(
        omop_url(w, 'source-code-rows'),
        {'table': table_key, 'code': code, 'vocabulary': vocabulary, **params})


def test_a_hospital_measurement_code_lists_its_rows_with_value_unit_and_status(world):
    w = world
    glucose = w['glucose'].concept_id
    a = measurement(w, glucose, date(2024, 1, 1), when=utc(2024, 1, 1, 8, 0), value=5.5, source='GLU-1')
    b = measurement(w, 0, date(2024, 2, 1), value=6, source='GLU-1')
    c = measurement(w, glucose, date(2024, 3, 1), value=7, source='GLU-1', confidence=70)
    d = measurement(w, glucose, date(2024, 4, 1), value=8, source='GLU-1', erroneous=True)
    measurement(w, glucose, date(2024, 5, 1), source='GLU-2')                       # another code
    measurement(w, glucose, date(2024, 5, 2), source='GLU-1', person=PersonFactory())  # another patient

    resp = any_code_rows(w, 'measurement', 'GLU-1', 'HT-Epic')

    assert resp.status_code == 200
    assert [(r['id'], r['value_as_number'], r['unit'], r['status'], r['is_erroneous'])
            for r in resp.data['rows']] == [
        (d.pk, 8.0, 'mg/dL', 'accepted', True),
        (c.pk, 7.0, 'mg/dL', 'provisional', False),
        (b.pk, 6.0, 'mg/dL', 'unresolved', False),
        (a.pk, 5.5, 'mg/dL', 'accepted', False)]
    assert resp.data['rows'][-1]['datetime'] == '2024-01-01T08:00:00+00:00'
    # No vocabulary: a code the list aggregated from the clinical tables has none.
    assert len(any_code_rows(w, 'measurement', 'GLU-1').data['rows']) == 4


def test_a_code_is_matched_as_a_row_holds_it_cut_to_the_column_and_without_case(world):
    w = world
    row = measurement(w, 0, date(2024, 1, 1), source='Glu-One')
    long_code = 'L' * 70
    long_row = measurement(w, 0, date(2024, 1, 2), source='L' * 50)
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'GLU-ONE').data['rows']] == [row.pk]
    assert [r['id'] for r in any_code_rows(w, 'measurement', long_code).data['rows']] == [long_row.pk]


def test_a_condition_code_shows_its_day_or_moment_and_status_and_no_value(world):
    w = world
    a = condition(w, date(2024, 1, 1), when=utc(2024, 1, 1, 9, 0))
    b = condition(w, date(2024, 2, 1), confidence=80, concept=w['glucose'].concept_id)
    c = condition(w, date(2024, 3, 1), erroneous=True)
    condition(w, date(2024, 4, 1), source='OTHER')

    resp = any_code_rows(w, 'condition', 'C50.9')

    assert [(r['id'], r['date'], r['datetime'], r['status'], r['value_as_number'], r['value_as_string'],
             r['unit'], r['is_erroneous']) for r in resp.data['rows']] == [
        (c.pk, '2024-03-01', None, 'unresolved', None, None, None, True),
        (b.pk, '2024-02-01', None, 'provisional', None, None, None, False),
        (a.pk, '2024-01-01', '2024-01-01T09:00:00+00:00', 'unresolved', None, None, None, False)]


def test_a_drug_code_shows_its_quantity_and_dose_unit(world):
    w = world
    a = drug(w, date(2024, 1, 1), quantity=Decimal('2.50000'), unit='mg')
    b = drug(w, date(2024, 2, 1))
    drug(w, date(2024, 3, 1), source='RX-2')
    resp = any_code_rows(w, 'drug_exposure', 'RX-1', 'RxNorm')
    assert [(r['id'], r['value_as_number'], r['unit']) for r in resp.data['rows']] == [
        (b.pk, None, None), (a.pk, 2.5, 'mg')]


def test_a_procedure_code_shows_its_quantity(world):
    w = world
    a = procedure(w, date(2024, 1, 1), quantity=3)
    procedure(w, date(2024, 1, 2), source='PX-2')
    resp = any_code_rows(w, 'procedure', 'PX-1')
    assert [(r['id'], r['value_as_number'], r['unit'], r['status']) for r in resp.data['rows']] == [
        (a.pk, 3.0, None, 'unresolved')]


def test_a_device_and_a_hospital_sharing_a_spelling_each_list_only_their_own_rows(world):
    w = world
    hold(w, 'spo2', 'Garmin', 'measurement')
    hold(w, 'SPO2', 'HT-Epic', 'measurement')
    device = measurement(w, 0, date(2024, 1, 2), source='spo2', value=97, type_id=WEARABLE_TYPE_CONCEPT_ID)
    local = measurement(w, 0, date(2024, 1, 3), source='SPO2', value=88)

    # The device's code lists the importer's rows, never the hospital's.
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'spo2', 'Garmin').data['rows']] == [device.pk]
    # The hospital's code leaves a device holder's wearable rows out (and matches without case).
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'SPO2', 'HT-Epic').data['rows']] == [local.pk]
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'SPO2').data['rows']] == [local.pk]


def test_a_hospital_row_of_the_importers_type_is_listed_when_the_patient_holds_no_device_code(world):
    w = world
    mine = measurement(w, 0, date(2024, 1, 2), source='SPO2', type_id=WEARABLE_TYPE_CONCEPT_ID)
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'SPO2', 'HT-Epic').data['rows']] == [mine.pk]
    # Another patient holding the device code changes nothing for this one.
    stranger = PersonFactory()
    PatientSourceCode.objects.create(
        person=stranger, source_value='spo2', source_vocabulary_id='Garmin', omop_table='measurement',
        occurrence_count=1)
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'SPO2', 'HT-Epic').data['rows']] == [mine.pk]


def test_a_device_spelling_held_as_an_observation_does_not_hide_a_measurements_rows(world):
    w = world
    hold(w, 'steps', 'Garmin', 'observation')
    row = measurement(w, 0, date(2024, 1, 2), source='steps', type_id=WEARABLE_TYPE_CONCEPT_ID)
    assert [r['id'] for r in any_code_rows(w, 'measurement', 'steps', 'HT-Epic').data['rows']] == [row.pk]


def test_any_code_is_read_in_pages_with_no_repeats(world):
    w = world
    made = [condition(w, date(2024, 1, 1 + i % 5), when=utc(2024, 1, 1 + i % 5, 8, 0) if i % 2 else None)
            for i in range(13)]
    expected = [r['id'] for r in any_code_rows(w, 'condition', 'C50.9', limit=200).data['rows']]
    assert len(expected) == len(made)
    seen, cursor = [], None
    while True:
        resp = any_code_rows(w, 'condition', 'C50.9', limit=5, **({'cursor': cursor} if cursor else {}))
        seen += [r['id'] for r in resp.data['rows']]
        cursor = resp.data['next_cursor']
        if not cursor:
            break
    assert seen == expected


@pytest.mark.parametrize('who,expected', [
    ('staff', 200), ('org_admin', 200), ('doctor', 403), ('analyst', 404), ('patient', 403), ('outsider', 404),
])
def test_who_reads_a_hospital_codes_rows(world, who, expected):
    condition(world, date(2024, 1, 1))
    assert any_code_rows(world, 'condition', 'C50.9', who=world[who]).status_code == expected


# ── Cost does not grow with the rows ─────────────────────────────────────────────────


def _count(client, url, params=None):
    with CaptureQueriesContext(connection) as queries:
        resp = client.get(url, params or {})
    assert resp.status_code == 200, resp.data
    return len(queries)


def _fill(w, n, start):
    ConditionOccurrence.objects.bulk_create([
        ConditionOccurrence(
            condition_occurrence_id=start + i, person=w['person'], condition_concept_id=0,
            condition_type_concept_id=HOSPITAL_TYPE_ID, condition_start_date=date(2024, 1, 1) + timedelta(days=i % 90),
            condition_source_value='C50.9') for i in range(n)])
    DrugExposure.objects.bulk_create([
        DrugExposure(
            drug_exposure_id=start + i, person=w['person'], drug_concept_id=0,
            drug_type_concept_id=HOSPITAL_TYPE_ID, drug_exposure_start_date=date(2024, 1, 1) + timedelta(days=i % 90),
            drug_exposure_end_date=date(2024, 1, 1), drug_source_value='RX-1', quantity=i) for i in range(n)])
    ProcedureOccurrence.objects.bulk_create([
        ProcedureOccurrence(
            procedure_occurrence_id=start + i, person=w['person'], procedure_concept_id=0,
            procedure_type_concept_id=HOSPITAL_TYPE_ID, procedure_date=date(2024, 1, 1) + timedelta(days=i % 90),
            procedure_source_value='PX-1', quantity=i) for i in range(n)])
    concepts = [w['glucose'].concept_id, w['rest_hr'].concept_id, 0]
    Measurement.objects.bulk_create([
        Measurement(
            measurement_id=start + i, person=w['person'], measurement_concept_id=concepts[i % 3],
            measurement_date=date(2024, 1, 1) + timedelta(days=i % 200),
            measurement_type_concept_id=WEARABLE_TYPE_CONCEPT_ID, value_as_number=i,
            measurement_source_value=f'code-{i % 7}') for i in range(n)])
    Observation.objects.bulk_create([
        Observation(
            observation_id=start + i, person=w['person'], observation_concept_id=concepts[i % 3],
            observation_date=date(2024, 1, 1) + timedelta(days=i % 200),
            observation_type_concept_id=WEARABLE_TYPE_CONCEPT_ID, value_as_number=i,
            observation_source_value='steps') for i in range(n)])


def test_the_queries_do_not_grow_with_the_rows(held_wearable_code):
    w = held_wearable_code
    client = client_as(w['staff'])
    urls = [
        (omop_url(w), {}),
        (omop_url(w, 'omop-rows'), {'table': 'measurements', 'concept': w['glucose'].concept_id}),
        (omop_url(w, 'omop-rows'), {'table': 'measurements', 'concept': 0, 'source_value': 'code-1'}),
        (omop_url(w, 'source-code-rows'), {'table': 'observation', 'code': 'steps', 'vocabulary': 'Garmin'}),
        (omop_url(w, 'source-code-rows'), {'table': 'measurement', 'code': 'code-1', 'vocabulary': 'HT-Epic'}),
        (omop_url(w, 'source-code-rows'), {'table': 'condition', 'code': 'C50.9'}),
        (omop_url(w, 'source-code-rows'), {'table': 'drug_exposure', 'code': 'RX-1'}),
        (omop_url(w, 'source-code-rows'), {'table': 'procedure', 'code': 'PX-1'}),
    ]
    _fill(w, 12, 90_000_000)
    small = [_count(client, url, params) for url, params in urls]
    _fill(w, 600, 91_000_000)
    large = [_count(client, url, params) for url, params in urls]
    assert small == large, (small, large)


def test_a_page_never_holds_more_than_the_limit_however_many_rows(held_wearable_code):
    w = held_wearable_code
    _fill(w, 300, 92_000_000)
    resp = client_as(w['staff']).get(
        omop_url(w, 'omop-rows'), {'table': 'measurements', 'concept': w['glucose'].concept_id})
    assert len(resp.data['rows']) == omop_row_pages.PAGE_SIZE
    assert resp.data['next_cursor']
