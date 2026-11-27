"""A login is linked to the Person an importer keyed to its (issuer, sub)."""
import threading

import pytest
from django.db import connection
from django.test import override_settings
from rest_framework.test import APIClient

from omop_core.models import Measurement, PatientRecord, Person
from omop_core.services.pk import next_pk
from patient_portal.models import Identity, PatientUser, PersonExternalKey
from patient_portal.services import resolve_or_create_person

ISS = 'https://securetoken.google.com/prod-project'
GRANTS = {
    'etl': {'token': 'etl-test-secret', 'scopes': 'patient/*.read system/etl.write'},
    'hk-labs': {'token': 'labs-test-secret', 'scopes': 'patient/*.read patient/*.write'},
}
URL = '/api/v1/persons/external-keys/'
pytestmark = [
    pytest.mark.django_db,
    pytest.mark.usefixtures('settings'),
]


@pytest.fixture(autouse=True)
def _config(settings):
    settings.SERVICE_AUTH_TOKENS = GRANTS
    settings.SERVICE_AUTH_TOKEN = ''
    settings.PERSON_EXTERNAL_KEY_ISSUERS = [ISS]


def _imported(**fields):
    person = Person.objects.create(person_id=next_pk(Person, 'person_id'), **fields)
    PatientRecord.objects.create(person=person)
    return person


def _login(sub, email=None):
    return Identity.objects.create(issuer=ISS, sub=sub, email=email or f'{sub}@example.com')


def _etl(secret='etl-test-secret'):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {secret}')
    return client


# --- the login side ---------------------------------------------------------

def test_first_login_links_the_keyed_person_without_an_email():
    imported = _imported()
    PersonExternalKey.objects.create(person=imported, issuer=ISS, subject='uid-1')

    person = resolve_or_create_person(_login('uid-1'), email='uid-1@example.com', email_verified=True)

    assert person.person_id == imported.person_id
    assert Person.objects.count() == 1


def test_first_login_links_a_person_keyed_on_actor_columns():
    imported = _imported(actor_iss=ISS, actor_sub='uid-2')

    person = resolve_or_create_person(_login('uid-2'))

    assert person.person_id == imported.person_id


def test_key_wins_over_an_email_match():
    keyed = _imported()
    PersonExternalKey.objects.create(person=keyed, issuer=ISS, subject='uid-3')
    _imported(email='uid-3@example.com')

    person = resolve_or_create_person(_login('uid-3'), email='uid-3@example.com', email_verified=True)

    assert person.person_id == keyed.person_id


def test_issuer_must_match_exactly():
    imported = _imported(actor_iss='https://securetoken.google.com/demo-project', actor_sub='uid-4')

    person = resolve_or_create_person(_login('uid-4'), email='', email_verified=False)

    assert person.person_id != imported.person_id


def test_an_empty_stub_heals_on_the_next_login():
    identity = _login('uid-5')
    stub = resolve_or_create_person(identity, email='', email_verified=False)
    imported = _imported()
    PersonExternalKey.objects.create(person=imported, issuer=ISS, subject='uid-5')

    person = resolve_or_create_person(identity)

    assert person.person_id == imported.person_id
    assert PatientUser.objects.get(identity=identity).person_id == imported.person_id
    assert not Person.objects.filter(person_id=stub.person_id).exists()


def test_a_stub_with_data_is_kept_and_reported():
    from omop_core.models import PersonMergeCandidate
    identity = _login('uid-6')
    stub = resolve_or_create_person(identity, email='', email_verified=False)
    Measurement.objects.create(
        measurement_id=next_pk(Measurement, 'measurement_id'), person=stub,
        measurement_concept_id=0, measurement_date='2026-01-01', measurement_type_concept_id=0,
    )
    imported = _imported()
    PersonExternalKey.objects.create(person=imported, issuer=ISS, subject='uid-6')

    assert resolve_or_create_person(identity).person_id == stub.person_id
    assert PersonMergeCandidate.objects.filter(claimed=stub, provisioned=imported).count() == 1
    # Settled: the next request neither re-files nor moves anything.
    assert resolve_or_create_person(identity).person_id == stub.person_id
    assert PersonMergeCandidate.objects.count() == 1


def test_a_keyed_person_another_account_holds_is_left_alone():
    imported = _imported()
    PersonExternalKey.objects.create(person=imported, issuer=ISS, subject='uid-7')
    other = Identity.objects.create(issuer='https://elsewhere.example', sub='x')
    PatientUser.objects.create(identity=other, person=imported)

    person = resolve_or_create_person(_login('uid-7'), email='', email_verified=False)

    assert person.person_id != imported.person_id
    assert PatientUser.objects.get(identity=other).person_id == imported.person_id


@pytest.mark.django_db(transaction=True)
def test_parallel_first_requests_by_email_do_not_fail():
    """Two first-page requests raced on the email branch's PatientUser insert."""
    _imported(email='race@example.com')
    identity = _login('uid-race', 'race@example.com')
    barrier, errors, got = threading.Barrier(2), [], []

    def first_request():
        try:
            barrier.wait()
            got.append(resolve_or_create_person(
                Identity.objects.get(pk=identity.pk), email='race@example.com', email_verified=True,
            ).person_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=first_request) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(set(got)) == 1
    assert PatientUser.objects.filter(identity=identity).count() == 1


# --- the importer side ------------------------------------------------------

def test_etl_records_keys_idempotently():
    a, b = _imported(), _imported()
    body = {'keys': [{'person_id': a.person_id, 'issuer': ISS, 'subject': 'k-a'},
                     {'person_id': b.person_id, 'issuer': ISS, 'subject': 'k-b'}]}

    first = _etl().post(URL, body, format='json')
    second = _etl().post(URL, body, format='json')

    assert first.status_code == 200, first.data
    assert (first.data['created'], second.data['created'], second.data['existing']) == (2, 0, 2)
    assert PersonExternalKey.objects.count() == 2


def test_a_key_is_never_moved():
    a, b = _imported(), _imported()
    PersonExternalKey.objects.create(person=a, issuer=ISS, subject='k')
    c = _imported(actor_iss=ISS, actor_sub='legacy')

    response = _etl().post(URL, {'keys': [
        {'person_id': b.person_id, 'issuer': ISS, 'subject': 'k'},
        {'person_id': b.person_id, 'issuer': ISS, 'subject': 'legacy'},
        {'person_id': 99999999, 'issuer': ISS, 'subject': 'none'},
    ]}, format='json')

    assert [r['status'] for r in response.data['results']] == ['conflict', 'conflict', 'not_found']
    assert [r.get('held_by') for r in response.data['results'][:2]] == [a.person_id, c.person_id]
    assert PersonExternalKey.objects.get(subject='k').person_id == a.person_id


def test_a_person_another_login_holds_is_not_keyed():
    person = _imported()
    PatientUser.objects.create(identity=_login('owner'), person=person)

    response = _etl().post(URL, {'keys': [
        {'person_id': person.person_id, 'issuer': ISS, 'subject': 'someone-else'},
        {'person_id': person.person_id, 'issuer': ISS, 'subject': 'owner'},
    ]}, format='json')

    assert [r['status'] for r in response.data['results']] == ['claimed', 'created']


@pytest.mark.parametrize('body', [
    {}, {'keys': []}, {'keys': ['x']},
    {'keys': [{'person_id': '1', 'issuer': ISS, 'subject': 's'}]},
    {'keys': [{'person_id': 1, 'issuer': 'https://not-allowed.example', 'subject': 's'}]},
    {'keys': [{'person_id': 1, 'issuer': ISS, 'subject': ' s'}]},
    {'keys': [{'person_id': 1, 'issuer': ISS, 'subject': 's'},
              {'person_id': 2, 'issuer': ISS, 'subject': 's'}]},
])
def test_a_bad_batch_writes_nothing(body):
    _imported()
    response = _etl().post(URL, body, format='json')
    assert response.status_code == 400, response.data
    assert not PersonExternalKey.objects.exists()


def test_only_the_etl_credential_may_write_keys(django_user_model):
    person = _imported()
    body = {'keys': [{'person_id': person.person_id, 'issuer': ISS, 'subject': 's'}]}
    staff = APIClient()
    staff.force_authenticate(Identity.objects.create(issuer='urn:local', sub='staff', is_staff=True))

    assert _etl('labs-test-secret').post(URL, body, format='json').status_code == 403
    assert staff.post(URL, body, format='json').status_code == 403
    assert APIClient().post(URL, body, format='json').status_code in (401, 403)
    assert _etl().post('/api/persons/external-keys/', body, format='json').status_code == 404
    assert not PersonExternalKey.objects.exists()


def test_consents_given_on_the_stub_follow_the_login():
    from datetime import timedelta

    from django.utils import timezone

    from patient_portal import consent as svc
    from patient_portal.models import (
        ConsentEvent, ConsentPurpose, ConsentText, DocumentAcceptance, LegalDocument, LegalDocumentVersion,
    )

    earlier = timezone.now() - timedelta(days=1)
    purpose = ConsentPurpose.objects.create(key='my_record', kind='permission')
    ConsentText.objects.create(purpose=purpose, version='1', language='en', title='t', body='.', published_at=earlier)
    terms = LegalDocument.objects.create(key='terms', name='Terms')
    LegalDocumentVersion.objects.create(document=terms, version='1', language='en', title='t', body='.',
                                        published_at=earlier)
    identity = _login('uid-consent')
    stub = resolve_or_create_person(identity, email='', email_verified=False)
    svc.record_consent(stub, 'my_record', 'granted', version='1', recorded_at=earlier)
    svc.accept_document(stub, 'terms', version='1')
    imported = _imported()
    PersonExternalKey.objects.create(person=imported, issuer=ISS, subject='uid-consent')

    assert resolve_or_create_person(identity).person_id == imported.person_id
    event = ConsentEvent.objects.get(person=imported)
    assert (event.decision, event.recorded_at) == ('granted', earlier)
    assert DocumentAcceptance.objects.filter(person=imported, version='1').count() == 1
    assert not Person.objects.filter(person_id=stub.person_id).exists()


def test_a_failed_move_leaves_the_login_where_it_was(monkeypatch):
    from patient_portal import person_adoption
    identity = _login('uid-fail')
    stub = resolve_or_create_person(identity, email='', email_verified=False)
    PersonExternalKey.objects.create(person=_imported(), issuer=ISS, subject='uid-fail')

    def broken(*args, **kwargs):
        raise RuntimeError('boom')
    monkeypatch.setattr(person_adoption, '_adopt', broken)

    assert resolve_or_create_person(identity).person_id == stub.person_id
