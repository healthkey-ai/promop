"""#2453: the (person, source_record_key) unique constraint on measurement and
observation."""
from datetime import date

import pytest
from django.db import IntegrityError, transaction

from omop_core.models import Measurement, Observation
from tests.factories import ConceptFactory, PersonFactory


pytestmark = pytest.mark.django_db


def _row(model, pk, person, concept, key):
    if model is Measurement:
        return Measurement(
            measurement_id=pk, person=person, measurement_concept=concept,
            measurement_type_concept=concept, measurement_date=date(2026, 11, 1),
            source_record_key=key,
        )
    return Observation(
        observation_id=pk, person=person, observation_concept=concept,
        observation_type_concept=concept, observation_date=date(2026, 11, 1),
        source_record_key=key,
    )


@pytest.mark.parametrize('model', [Measurement, Observation])
def test_unkeyed_rows_never_collide(model):
    person, concept = PersonFactory(), ConceptFactory()
    model.objects.bulk_create(
        [_row(model, pk, person, concept, None) for pk in (1, 2)])
    assert model.objects.filter(person=person).count() == 2


@pytest.mark.parametrize('model', [Measurement, Observation])
def test_a_key_is_unique_per_person(model):
    person, other, concept = PersonFactory(), PersonFactory(), ConceptFactory()
    key = 'urn:issuer:facility/Observation/abc'
    model.objects.bulk_create([
        _row(model, 1, person, concept, key),
        _row(model, 2, other, concept, key),
    ])
    with pytest.raises(IntegrityError), transaction.atomic():
        model.objects.bulk_create([_row(model, 3, person, concept, key)])


# --- keyed upsert through the API --------------------------------------------

from decimal import Decimal  # noqa: E402

from django.contrib.contenttypes.models import ContentType  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from omop_core.models import Organization, ProvenanceRecord  # noqa: E402
from patient_portal.api import views  # noqa: E402
from patient_portal.models import Identity  # noqa: E402
from tests.factories import PatientRecordFactory  # noqa: E402

KEY = 'urn:oid:1.2.840:FAC/Observation/abc-1'


@pytest.fixture
def client(db):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='source-record-key@example.org', password='test', is_staff=True))
    return client


@pytest.fixture
def api(db, client):
    person = PersonFactory()
    PatientRecordFactory(person=person)
    concept = ConceptFactory(concept_code='8310-5', concept_name='Body temperature')
    other = ConceptFactory(concept_code='6', concept_name='Temperature (local)')
    type_concept = ConceptFactory(concept_code='EHR-2453', concept_name='EHR')

    def row(key=KEY, value='37.1', source_value='8310-5', concept_=None, **extra):
        data = {
            'person': person.person_id,
            'measurement_concept': (concept_ or concept).concept_id,
            'measurement_date': '2026-11-01',
            'measurement_datetime': '2026-11-01T08:00:00Z',
            'measurement_type_concept': type_concept.concept_id,
            'measurement_source_value': source_value,
            'value_as_number': value, 'value_source_value': value,
            'unit_source_value': 'Cel', **extra,
        }
        if key is not None:
            data['source_record_key'] = key
        return data

    def post(rows, query='', **headers):
        return client.post(f'/api/v1/measurements/?skip_refresh=true{query}',
                           rows, format='json', **headers)

    api = type('Api', (), {})()
    api.person, api.concept, api.other, api.row, api.post = person, concept, other, row, post
    return api


def _rows(person):
    return Measurement.objects.filter(person=person).order_by('measurement_id')


def test_a_replay_under_another_code_rewrites_the_keyed_row(api):
    first = api.post([api.row(source_value='6', concept_=api.other)])
    assert first.status_code == 201, first.content

    again = api.post([api.row(value='37.4')])

    assert again.status_code == 201, again.content
    body = again.json()
    assert (body['created'], body['updated'], body['key_ambiguous']) == (0, 1, [])
    assert body['ids'] == first.json()['ids']
    row = _rows(api.person).get()
    assert (row.measurement_source_value, row.measurement_concept_id,
            row.value_as_number, row.value_source_value) == (
        '8310-5', api.concept.concept_id, Decimal('37.4'), '37.4')


def test_a_key_match_keeps_the_entered_in_error_flag(api):
    api.post([api.row()])
    Measurement.objects.filter(person=api.person).update(
        is_erroneous=True, erroneous_reason='wrong patient')

    resp = api.post([api.row(value='38.0')])

    assert resp.status_code == 201, resp.content
    row = _rows(api.person).get()
    assert (row.is_erroneous, row.erroneous_reason, row.value_as_number) == (
        True, 'wrong patient', Decimal('38.0'))


def test_an_unkeyed_row_matched_by_content_is_stamped(api):
    stored = api.post([api.row(key=None)]).json()['ids']

    resp = api.post([api.row()])

    assert resp.json()['ids'] == stored
    assert resp.json()['key_ambiguous'] == []
    assert _rows(api.person).get().source_record_key == KEY


def test_two_keys_on_one_unkeyed_row_are_ambiguous(api):
    stored = api.post([api.row(key=None)]).json()['ids'][0]

    resp = api.post([api.row(key='k/1'), api.row(key='k/2')])

    body = resp.json()
    assert body['key_ambiguous'] == [0, 1]
    assert body['ids'][1] == stored          # the last maps to the stored row
    assert body['created'] == 1
    keys = dict(_rows(api.person).values_list('measurement_id', 'source_record_key'))
    assert keys[stored] is None              # nothing stamped
    assert keys[body['ids'][0]] == 'k/1'


def test_keyed_rows_with_identical_content_are_different_records(api):
    resp = api.post([api.row(key='k/1'), api.row(key='k/2')])

    assert resp.json()['created'] == 2
    assert _rows(api.person).count() == 2


def test_a_repeated_key_in_one_batch_is_one_row_last_wins(api):
    resp = api.post([api.row(value='36.9'), api.row(value='37.2')])

    body = resp.json()
    assert body['created'] == 1 and body['ids'][0] == body['ids'][1]
    assert _rows(api.person).get().value_as_number == Decimal('37.2')


def test_a_content_collapse_never_deletes_a_keyed_row(api):
    api.post([api.row(key='k/1')])
    api.post([api.row(key=None)], query='&upsert=false')
    assert _rows(api.person).count() == 2

    api.post([api.row(key=None)])

    assert list(_rows(api.person).values_list('source_record_key', flat=True)) == ['k/1']


def test_upsert_false_still_matches_by_key(api):
    first = api.post([api.row()], query='&upsert=false').json()['ids']

    resp = api.post([api.row(value='37.9'), api.row(key=None), api.row(key=None)],
                    query='&upsert=false')

    body = resp.json()
    assert body['ids'][0] == first[0]
    assert body['created'] == 2              # unkeyed rows stay append-only
    assert _rows(api.person).get(source_record_key=KEY).value_as_number == Decimal('37.9')


def test_single_row_post_matches_by_key(api, client):
    first = api.post(api.row())
    assert first.status_code == 201, first.content

    again = api.post(api.row(value='37.6', source_value='6', concept_=api.other))

    assert again.status_code == 200, again.content
    assert again.json()['measurement_id'] == first.json()['measurement_id']
    assert again.json()['source_record_key'] == KEY
    assert _rows(api.person).get().value_as_number == Decimal('37.6')


@pytest.mark.parametrize('bad', ['', 'has space', 'tab\there', 'ключ', 'x' * 513])
def test_a_malformed_key_is_400(api, bad):
    resp = api.post([api.row(key=bad)])

    assert resp.status_code == 400, resp.content
    assert 'source_record_key' in resp.json()[0]


def test_another_hospitals_key_is_409(api):
    ids = api.post([api.row()]).json()['ids']
    hospital_a = Organization.objects.create(name='A', slug='srk-a')
    hospital_b = Organization.objects.create(name='B', slug='srk-b')
    ProvenanceRecord.objects.create(
        source='EHR_SYNC', content_type=ContentType.objects.get_for_model(Measurement),
        object_id=ids[0], organization=hospital_a)

    resp = api.post([api.row(value='39.0')], HTTP_X_PROVENANCE_SOURCE='EHR_SYNC',
                    HTTP_X_PROVENANCE_ORGANIZATION_ID=str(hospital_b.pk))

    assert resp.status_code == 409, resp.content
    assert _rows(api.person).get().value_as_number == Decimal('37.1')


def test_a_key_match_records_provenance_without_adding_a_hospital(api):
    ids = api.post([api.row()]).json()['ids']

    api.post([api.row(value='37.3')], HTTP_X_PROVENANCE_SOURCE='EHR_SYNC')

    record = ProvenanceRecord.objects.get(
        content_type=ContentType.objects.get_for_model(Measurement), object_id=ids[0])
    assert (record.source, record.organization_id) == ('EHR_SYNC', None)


def test_a_key_inserted_concurrently_becomes_an_update(api, monkeypatch):
    stored = api.post([api.row()]).json()['ids']
    real = views._keyed_rows
    calls = []

    def unseen_first(*args, **kwargs):
        calls.append(1)
        return {} if len(calls) == 1 else real(*args, **kwargs)

    # The plan's lookup misses the row, as if another request inserted it
    # after the SELECT; the INSERT then conflicts and is applied as an update.
    monkeypatch.setattr(views, '_keyed_rows', unseen_first)
    resp = api.post([api.row(value='37.8')], query='&upsert=false')

    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert (body['created'], body['updated'], body['ids']) == (0, 1, stored)
    assert _rows(api.person).get().value_as_number == Decimal('37.8')


def test_a_stamp_candidate_is_locked(api):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    api.post([api.row(key=None)])
    with CaptureQueriesContext(connection) as ctx:
        resp = api.post([api.row()])

    assert resp.status_code == 201, resp.content
    # A concurrent request with another key waits on this lock, then sees the
    # row keyed and inserts instead of overwriting the stamp.
    assert any('"measurement_source_value" IN' in q['sql'] and 'FOR UPDATE' in q['sql']
               for q in ctx.captured_queries)


def test_bulk_update_may_set_a_key_and_a_collision_is_409(api, client):
    a, b = api.post([api.row(key=None, value='36.1'),
                     api.row(key=None, value='36.2')]).json()['ids']
    url = '/api/v1/measurements/bulk_update/?skip_refresh=true'

    ok = client.patch(url, [{'measurement_id': a, 'source_record_key': 'k/a'}], format='json')
    clash = client.patch(url, [{'measurement_id': b, 'source_record_key': 'k/a'}], format='json')

    assert ok.status_code == 200, ok.content
    assert clash.status_code == 409, clash.content


def _attribute(pk, organization):
    ProvenanceRecord.objects.create(
        source='EHR_SYNC', content_type=ContentType.objects.get_for_model(Measurement),
        object_id=pk, organization=organization, source_user_id=f'org-{organization.pk}')


def test_unkeyed_copies_of_two_hospitals_are_ambiguous_without_a_header(api):
    a, b = (Organization.objects.create(name=n, slug=f'srk-{n}') for n in 'ab')
    first, second = api.post([api.row(key=None)], query='&upsert=false').json()['ids'] + \
        api.post([api.row(key=None)], query='&upsert=false').json()['ids']
    _attribute(first, a)
    _attribute(second, b)

    resp = api.post([api.row()])

    assert resp.json()['key_ambiguous'] == [0]
    assert not _rows(api.person).filter(source_record_key=KEY).exists()


def test_with_a_header_only_the_hospitals_own_row_is_a_candidate(api):
    a, b = (Organization.objects.create(name=n, slug=f'srk-{n}') for n in 'ab')
    first, second = api.post([api.row(key=None)], query='&upsert=false').json()['ids'] + \
        api.post([api.row(key=None)], query='&upsert=false').json()['ids']
    _attribute(first, a)
    _attribute(second, b)

    resp = api.post([api.row()], HTTP_X_PROVENANCE_SOURCE='EHR_SYNC',
                    HTTP_X_PROVENANCE_ORGANIZATION_ID=str(a.pk))

    assert resp.status_code == 201, resp.content
    assert (resp.json()['ids'], resp.json()['key_ambiguous']) == ([first], [])
    assert _rows(api.person).get(source_record_key=KEY).pk == first
