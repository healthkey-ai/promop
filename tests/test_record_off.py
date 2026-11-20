"""While the permission a record depends on is withdrawn, nothing written on
the member's behalf is kept (patient_portal.consent.record_permitted)."""
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from omop_core.models import PatientRecord
from patient_portal import consent as svc
from patient_portal.models import ConsentEvent
from prolog_surveys.models import SurveyResponse
from tests.test_member_erasure import member, purpose, withdraw  # noqa: F401 (fixtures)
from tests.test_survey_record_mapping import ANSWERS, SLUG, mapped, patient, submit  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def off(member):
    assert withdraw(member['client'], 'my_record').status_code == 200
    assert svc.record_permitted(member['person']) is False
    return member


def start(client):
    return client.post('/api/v1/prolog/run/responses/', {'slug': SLUG, 'language': 'en'}, format='json')


def test_a_record_is_permitted_until_its_permission_is_withdrawn_and_again_once_given(member):
    person = member['person']
    assert svc.record_permitted(person) is True
    svc.record_consent(person, 'my_record', ConsentEvent.WITHDRAWN, version='1.0')
    assert svc.record_permitted(person) is False
    svc.record_consent(person, 'my_record', ConsentEvent.GRANTED, version='1.0')
    assert svc.record_permitted(person) is True


def test_without_a_marked_purpose_every_record_is_permitted(member):
    from patient_portal.models import ConsentPurpose

    ConsentPurpose.objects.update(erases_record_on_withdrawal=False)
    svc.record_consent(member['person'], 'my_record', ConsentEvent.WITHDRAWN, version='1.0')

    assert svc.record_permitted(member['person']) is True


def test_no_questionnaire_is_started_while_the_record_is_off(member):
    assert start(member['client']).status_code in (200, 201)
    withdraw(member['client'], 'my_record')

    assert start(member['client']).status_code == 403


def test_a_response_submitted_while_off_is_not_written_to_the_record(off, mapped, django_capture_on_commit_callbacks):  # noqa: F811
    submit(mapped, off['record'], ANSWERS, django_capture_on_commit_callbacks)

    assert PatientRecord.objects.get(person=off['person']).stage in (None, '')


def test_the_member_cannot_edit_the_record_but_can_still_change_their_name(off):
    client = off['client']

    assert client.patch('/api/v1/patient-records/me/', {'stage': 'III'}, format='json').status_code == 403
    assert client.patch('/api/v1/patient-records/me/', {'patient_name': 'Sam Taylor'}, format='json').status_code == 200
    assert PatientRecord.objects.get(person=off['person']).stage in (None, '')


def test_phr_writes_are_refused_and_deletes_are_not(off):
    client = off['client']

    assert client.post('/api/v1/phr/medications/add/', {'name': 'Rituximab'}, format='json').status_code == 403
    assert client.post('/api/v1/phr/shares/', {}, format='json').status_code == 403
    assert client.delete('/api/v1/phr/shares/0/').status_code != 403


def test_health_data_synced_or_uploaded_by_the_member_is_refused(off):
    client = off['client']
    bundle = {'bundle': {'resourceType': 'Bundle', 'type': 'collection', 'entry': []}}

    assert client.post('/api/fhir/patient-sync/', bundle, format='json').status_code == 403
    upload = SimpleUploadedFile('export.zip', b'PK\x05\x06' + b'\x00' * 18, content_type='application/zip')
    r = client.post('/api/v1/patient-records/upload-wearable/', {'file': upload, 'device_type': 'apple'})
    assert r.status_code == 403


def test_giving_the_permission_again_opens_the_record(off):
    client = off['client']
    r = client.post('/api/v1/phr/consent/', {'decisions': [
        {'purpose': 'my_record', 'decision': 'granted', 'version': '1.0'}]}, format='json')

    assert r.status_code == 200
    assert client.patch('/api/v1/patient-records/me/', {'stage': 'III'}, format='json').status_code == 200
    assert start(client).status_code in (200, 201)
    assert SurveyResponse.objects.filter(participant_id=off['person'].person_id).count() == 1
