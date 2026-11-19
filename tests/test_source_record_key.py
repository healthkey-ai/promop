"""#2453: the (person, source_record_key) unique constraint on measurement and
observation."""
from datetime import date

import pytest
from django.db import IntegrityError, transaction
from django.test import override_settings

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


def test_a_key_deleted_before_the_reread_is_409(api, monkeypatch):
    api.post([api.row()])
    # Both lookups miss: the plan's, and the re-read after the INSERT conflicted,
    # as if the blocking row was deleted in between.
    monkeypatch.setattr(views, '_keyed_rows', lambda *a, **k: {})

    resp = api.post([api.row(value='37.8')], query='&upsert=false')

    assert resp.status_code == 409, resp.content
    assert _rows(api.person).get().value_as_number == Decimal('37.1')


@pytest.mark.parametrize('key', ['k/new', None])
def test_an_insert_whose_id_is_taken_is_409(api, monkeypatch, key):
    # The id clashes with a row of the same person (keyed or not), as a writer
    # allocating outside the sequence could cause. A keyed row elsewhere in the
    # batch turns on ON CONFLICT DO NOTHING, which must not hide this conflict.
    taken = api.post([api.row(key=None, value='36.0')]).json()['ids'][0]
    monkeypatch.setattr(views, 'next_pk_batch', lambda *a, **k: iter([taken, taken + 10**9]))

    resp = api.post([api.row(key=key, value='36.5'), api.row(key='k/other', value='36.9')],
                    query='&upsert=false')

    assert resp.status_code == 409, resp.content
    assert list(_rows(api.person).values_list('measurement_id', flat=True)) == [taken]


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


# --- real rows survive a proposal's duplicate collapse (#2543) ----------------

def _stored_measurement(person, concept, type_concept, pk, confidence):
    """A stored row with no key, so the batch matches it by content."""
    from datetime import datetime, timezone as dt_timezone

    return Measurement.objects.create(
        measurement_id=pk,
        person=person,
        measurement_concept=concept,
        measurement_type_concept=type_concept,
        measurement_date=date(2026, 11, 1),
        measurement_datetime=datetime(2026, 11, 1, 8, 0, tzinfo=dt_timezone.utc),
        measurement_source_value='8310-5',
        value_as_number=Decimal('37.1'),
        value_source_value='37.1',
        unit_source_value='Cel',
        confidence=confidence,
    )


@override_settings(PROVISIONAL_DATA_ENABLED=True)
def test_a_proposal_keeps_a_later_real_row_when_it_collapses_duplicates(api):
    """The earliest row is provisional, the approved fact comes later. A new
    proposal must keep the real row and must not rewrite it (#2543)."""
    type_concept = ConceptFactory(concept_code='EHR-2543', concept_name='EHR')
    _stored_measurement(api.person, api.concept, type_concept, 9001, 50)
    _stored_measurement(api.person, api.concept, type_concept, 9002, None)

    response = api.post([api.row(value='37.1', confidence=50, key=None)])

    assert response.status_code == 201, response.content
    rows = list(_rows(api.person))
    assert [row.measurement_id for row in rows] == [9002]
    assert rows[0].confidence is None
    assert response.json()['ids'] == [9002]


@override_settings(PROVISIONAL_DATA_ENABLED=True)
def test_a_proposal_keeps_a_later_real_row_in_the_keyed_fallback(api):
    """The keyed fallback stamps the key on one content candidate; when a real
    candidate exists, that one is kept and the proposal does not rewrite it."""
    type_concept = ConceptFactory(concept_code='EHR-2543', concept_name='EHR')
    _stored_measurement(api.person, api.concept, type_concept, 9003, 50)
    _stored_measurement(api.person, api.concept, type_concept, 9004, None)

    response = api.post([api.row(value='37.1', confidence=50, key=KEY)])

    assert response.status_code == 201, response.content
    rows = list(_rows(api.person))
    assert [row.measurement_id for row in rows] == [9004]
    assert rows[0].confidence is None
    assert rows[0].source_record_key is None
    assert response.json()['ids'] == [9004]


@override_settings(PROVISIONAL_DATA_ENABLED=True)
def test_a_real_row_first_still_collapses_a_later_proposal(api):
    """The reversed order was already safe; keep it pinned."""
    type_concept = ConceptFactory(concept_code='EHR-2543', concept_name='EHR')
    _stored_measurement(api.person, api.concept, type_concept, 9005, None)
    _stored_measurement(api.person, api.concept, type_concept, 9006, 50)

    response = api.post([api.row(value='37.1', confidence=50, key=None)])

    assert response.status_code == 201, response.content
    rows = list(_rows(api.person))
    assert [row.measurement_id for row in rows] == [9005]
    assert rows[0].confidence is None


@override_settings(PROVISIONAL_DATA_ENABLED=True)
def test_repeated_batch_rows_point_at_the_surviving_real_row(api):
    """Two rows of one batch share a content key; both returned ids must name the
    real row the last occurrence keeps, not the provisional one it collapses."""
    type_concept = ConceptFactory(concept_code='EHR-2543', concept_name='EHR')
    _stored_measurement(api.person, api.concept, type_concept, 9009, 50)
    _stored_measurement(api.person, api.concept, type_concept, 9010, None)

    response = api.post([
        api.row(value='37.1', confidence=50, key=None),
        api.row(value='37.1', confidence=50, key=None),
    ])

    assert response.status_code == 201, response.content
    assert response.json()['ids'] == [9010, 9010]
    rows = list(_rows(api.person))
    assert [row.measurement_id for row in rows] == [9010]
    assert rows[0].confidence is None


@override_settings(PROVISIONAL_DATA_ENABLED=True)
def test_the_earliest_row_is_kept_when_every_candidate_is_a_proposal(api):
    type_concept = ConceptFactory(concept_code='EHR-2543', concept_name='EHR')
    _stored_measurement(api.person, api.concept, type_concept, 9007, 50)
    _stored_measurement(api.person, api.concept, type_concept, 9008, 40)

    response = api.post([api.row(value='37.1', confidence=30, key=None)])

    assert response.status_code == 201, response.content
    rows = list(_rows(api.person))
    assert [row.measurement_id for row in rows] == [9007]
    assert rows[0].confidence == 30


# --- #2483: a keyed row must not hide an unkeyed copy -------------------------


def test_a_keyed_row_does_not_hide_an_unkeyed_copy_of_another_hospital(api):
    """The keyed fallback reads the unkeyed candidate even when attribution
    narrowing keeps only the keyed row (#2483)."""
    a, b = (Organization.objects.create(name=n, slug=f'hidden-{n}') for n in 'ab')
    keyed = api.post([api.row(key='k/one')]).json()['ids'][0]
    unkeyed = api.post([api.row(key=None)], query='&upsert=false').json()['ids'][0]
    _attribute(keyed, a)
    _attribute(unkeyed, b)

    resp = api.post([api.row(key='k/two')])

    body = resp.json()
    assert resp.status_code == 201, resp.content
    assert (body['created'], body['updated'], body['ids']) == (0, 1, [unkeyed])
    assert _rows(api.person).count() == 2
    keys = dict(Measurement.objects.filter(person=api.person)
                .values_list('measurement_id', 'source_record_key'))
    assert keys == {keyed: 'k/one', unkeyed: 'k/two'}


# --- #2484: row-level writes and the key -------------------------------------


def test_a_row_patch_onto_another_rows_key_is_409(api, client):
    first, second = api.post([api.row(key='k/a'), api.row(key='k/b')]).json()['ids']

    resp = client.patch(f'/api/v1/measurements/{second}/?skip_refresh=true',
                        {'source_record_key': 'k/a'}, format='json')

    assert resp.status_code == 409, resp.content
    assert Measurement.objects.get(pk=second).source_record_key == 'k/b'
    assert Measurement.objects.get(pk=first).source_record_key == 'k/a'


def test_a_row_patch_may_keep_its_own_key(api, client):
    pk = api.post([api.row(key='k/a')]).json()['ids'][0]

    resp = client.patch(f'/api/v1/measurements/{pk}/?skip_refresh=true',
                        {'source_record_key': 'k/a', 'value_as_number': '37.9'},
                        format='json')

    assert resp.status_code == 200, resp.content
    assert Measurement.objects.get(pk=pk).source_record_key == 'k/a'


@pytest.mark.parametrize('bad', [' k/a', 'k/a ', '\tk/a', 'k/a\n'])
def test_a_padded_key_on_a_row_patch_is_400(api, client, bad):
    """DRF's default trimming used to turn a padded key into a valid one
    (#2484); the field must see the supplied identifier."""
    pk = api.post([api.row(key=None)]).json()['ids'][0]

    resp = client.patch(f'/api/v1/measurements/{pk}/?skip_refresh=true',
                        {'source_record_key': bad}, format='json')

    assert resp.status_code == 400, resp.content
    assert Measurement.objects.get(pk=pk).source_record_key is None


def test_an_ambiguous_replay_keeps_every_hospitals_row(api):
    """Counting ambiguity must not collapse a row across attributions (#2483)."""
    a, b = (Organization.objects.create(name=n, slug=f'keep-{n}') for n in 'ab')
    first, second = (api.post([api.row(key=None)], query='&upsert=false').json()['ids'][0]
                     for _ in range(2))
    _attribute(first, a)
    _attribute(second, b)

    resp = api.post([api.row()])

    assert resp.json()['key_ambiguous'] == [0]
    assert _rows(api.person).count() == 2
    assert not _rows(api.person).filter(source_record_key=KEY).exists()
    assert ProvenanceRecord.objects.filter(
        object_id__in=(first, second)).count() == 2


def test_a_single_post_with_an_explicit_id_cannot_take_a_key(api, client):
    """A body carrying its own primary key skips the keyed upsert (#2484)."""
    api.post([api.row(key='k/a')])
    data = api.row(key='k/a')
    data['measurement_id'] = 999001

    resp = client.post('/api/v1/measurements/?skip_refresh=true', data, format='json')

    assert resp.status_code == 409, resp.content
    assert not Measurement.objects.filter(measurement_id=999001).exists()


def test_a_padded_key_in_a_bulk_batch_is_400(api):
    """The bulk ingest shares the key field, so padding is refused there too
    (#2484 review)."""
    resp = api.post([api.row(key='k/a ')])

    assert resp.status_code == 400, resp.content
    assert not _rows(api.person).exists()


def test_a_conflicting_row_patch_answers_409_without_refreshing(api, client, monkeypatch):
    """The 409 must stop the request: a returned response would still run the
    PatientRecord derivation, and turn into a false 502 when it fails
    (#2484 review)."""
    api.post([api.row(key='k/a'), api.row(key='k/b')])
    other = Measurement.objects.get(person=api.person, source_record_key='k/a')
    target = Measurement.objects.get(person=api.person, source_record_key='k/b')
    called = []

    def fail(*args, **kwargs):
        called.append((args, kwargs))
        raise RuntimeError('the derivation must not run for a refused write')

    monkeypatch.setattr(views, 'refresh_patient_record', fail, raising=False)

    resp = client.patch(f'/api/v1/measurements/{target.pk}/',
                        {'source_record_key': other.source_record_key}, format='json')

    assert resp.status_code == 409, resp.content
    assert called == []


def test_a_bulk_collision_answers_the_generic_409(api, client):
    """A bulk key collision must not echo PostgreSQL's message, which quotes
    the other row's key (#2484 review)."""
    api.post([api.row(key='k/a'), api.row(key='k/b')])
    url = '/api/v1/measurements/bulk_update/?skip_refresh=true'

    resp = client.patch(url, [
        {'measurement_id': Measurement.objects.get(source_record_key='k/b').pk,
         'source_record_key': 'k/a'},
    ], format='json')

    assert resp.status_code == 409, resp.content
    assert 'error' not in resp.json()
    assert 'k/a' not in resp.json()['detail']


@pytest.mark.parametrize('destination_first', [True, False])
def test_a_bulk_update_may_transfer_a_key_in_either_order(api, client, destination_first):
    """The batch releases the keys it changes before writing any group, so a
    destination group that sorts first cannot collide with the release (#2709)."""
    first, second = api.post([api.row(key='k/a'), api.row(key='k/b')]).json()['ids']
    rows = [
        {'measurement_id': second, 'source_record_key': 'k/a', 'value_as_number': '37.7'},
        {'measurement_id': first, 'source_record_key': None},
    ]
    if not destination_first:
        rows.reverse()

    resp = client.patch('/api/v1/measurements/bulk_update/?skip_refresh=true',
                        rows, format='json')

    assert resp.status_code == 200, resp.content
    keys = dict(Measurement.objects.filter(person=api.person)
                .values_list('measurement_id', 'source_record_key'))
    assert keys == {first: None, second: 'k/a'}
