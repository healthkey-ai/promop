"""Closing an account, and withdrawing the permission a record depends on
(patient_portal.erasure, docs/member_erasure_architecture.md)."""
import json

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.models import PatientDocument, PatientRecord, Person
from omop_core.services.field_write_service import write_person_location
from omop_oncology.models import Episode
from patient_portal import consent as svc
from patient_portal.models import (
    AuditEvent, ConsentEvent, ConsentLedgerEntry, ConsentPurpose, ConsentText, Identity, PatientUser,
)
from prolog_surveys.models import SurveyAnswer, SurveyResponse
from tests.test_consent_v2 import EARLIER
from tests.test_survey_record_mapping import ANSWERS, mapped, patient, submit  # noqa: F401 (fixtures)

pytestmark = pytest.mark.django_db


def purpose(key, **kw):
    p = ConsentPurpose.objects.create(key=key, **kw)
    ConsentText.objects.create(purpose=p, version='1.0', language='en', title=key, body='...', published_at=EARLIER)
    return p


@pytest.fixture
def member(mapped, patient, django_capture_on_commit_callbacks):  # noqa: F811
    """A member who joined (country, year), granted their record's permission
    and answered a questionnaire that wrote their record and a treatment line."""
    record, identity = patient
    write_person_location(record.person, {'country': 'GB'})
    purpose('my_record', erases_record_on_withdrawal=True)
    purpose('updates', kind='contact_preference')
    svc.record_consent(record.person, 'my_record', ConsentEvent.GRANTED, version='1.0')
    svc.record_consent(record.person, 'updates', ConsentEvent.GRANTED, version='1.0')
    submit(mapped, record, ANSWERS, django_capture_on_commit_callbacks)
    record.refresh_from_db()
    assert record.stage == 'III' and Episode.objects.filter(person=record.person).exists()
    client = APIClient()
    client.force_authenticate(identity)
    return {'record': record, 'person': record.person, 'identity': identity, 'client': client}


def withdraw(client, key):
    return client.post('/api/v1/phr/consent/', {'decisions': [{'purpose': key, 'decision': 'withdrawn', 'version': '1.0'}]},
                       format='json')


def erasures():
    """The deletion log: (who asked, the entry's detail), oldest first."""
    return [(e.user_id, e.detail) for e in AuditEvent.objects.filter(method='ERASE').order_by('id')]


def test_a_member_who_answered_a_questionnaire_can_close_their_account(member):
    person_id, identity_id = member['person'].person_id, member['identity'].pk
    ledger = ConsentLedgerEntry.objects.count()

    r = member['client'].delete('/api/v1/patient-records/me/', {'confirm': 'DELETE'}, format='json')

    assert r.status_code == 200
    assert not Person.objects.filter(person_id=person_id).exists()
    assert not PatientRecord.objects.filter(person_id=person_id).exists()
    assert not SurveyResponse.objects.filter(participant_id=person_id).exists()
    assert not SurveyAnswer.objects.exists()
    assert not Episode.objects.filter(person_id=person_id).exists()
    assert not Identity.objects.filter(pk=identity_id).exists()
    # Kept: the permission history in the ledger, and the deletion log naming
    # who, not what.
    assert ConsentLedgerEntry.objects.count() == ledger
    assert erasures() == [(str(identity_id), {'erasure': 'account', 'person_id': person_id, 'identity_id': identity_id})]
    assert AuditEvent.objects.get(method='ERASE').signature_valid()


def test_withdrawing_the_records_permission_deletes_the_health_answers_and_keeps_the_account(member):
    person, organization = member['person'], member['record'].organization

    r = withdraw(member['client'], 'my_record')

    assert r.status_code == 200
    record = PatientRecord.objects.get(person=person)
    assert record.stage in (None, '') and record.diagnosis_date is None
    assert not Episode.objects.filter(person=person).exists()
    assert not SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    # The account, the membership and the sign-up facts stay; so do the
    # member's other permissions and the whole history.
    assert PatientUser.objects.filter(person=person, identity=member['identity']).exists()
    assert record.organization == organization and record.country == 'GB'
    assert svc.has_consent(person, 'updates') is True
    assert [e.decision for e in ConsentEvent.objects.filter(person=person, purpose__key='my_record').order_by('id')] == [
        'granted', 'withdrawn']
    assert erasures() == [(str(member['identity'].pk), {'erasure': 'record', 'person_id': person.person_id, 'purpose': 'my_record'})]
    assert member['client'].get('/api/v1/phr/consent/').status_code == 200


def test_withdrawing_any_other_permission_deletes_nothing(member):
    person = member['person']

    assert withdraw(member['client'], 'updates').status_code == 200

    assert PatientRecord.objects.get(person=person).stage == 'III'
    assert SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    assert erasures() == []


def test_staff_deleting_a_member_who_answered_a_questionnaire(member):
    staff = Identity.objects.create_user(email='staff@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(staff)
    person_id = member['person'].person_id

    r = client.delete(f'/api/v1/patient-records/{person_id}/admin-delete/', {'confirm': 'DELETE'}, format='json')

    assert r.status_code == 200
    assert not Person.objects.filter(person_id=person_id).exists()
    assert not SurveyResponse.objects.filter(participant_id=person_id).exists()
    assert not Identity.objects.filter(pk=member['identity'].pk).exists()
    assert erasures() == [(str(staff.pk), {'erasure': 'account', 'person_id': person_id, 'identity_id': member['identity'].pk})]


def test_load_consent_texts_sets_which_purpose_erases_the_record(tmp_path):
    spec = {'purposes': [{'key': 'my_record', 'erases_record_on_withdrawal': True,
                          'texts': [{'version': '1.0', 'language': 'en', 'title': 'My record', 'body': '...'}]},
                         {'key': 'research',
                          'texts': [{'version': '1.0', 'language': 'en', 'title': 'Research', 'body': '...'}]}]}
    path = tmp_path / 'consent.json'
    path.write_text(json.dumps(spec))

    call_command('load_consent_texts', str(path))

    assert dict(ConsentPurpose.objects.values_list('key', 'erases_record_on_withdrawal')) == {
        'my_record': True, 'research': False}


def test_declining_a_granted_record_permission_erases_the_record_too(member):
    """Any answer that ends the grant ends the record, not only "withdrawn"."""
    person = member['person']

    r = member['client'].post('/api/v1/phr/consent/', {'decisions': [
        {'purpose': 'my_record', 'decision': 'declined', 'version': '1.0'}]}, format='json')

    assert r.status_code == 200 and svc.has_consent(person, 'my_record') is False
    assert PatientRecord.objects.get(person=person).stage in (None, '')
    assert not SurveyResponse.objects.filter(participant_id=person.person_id).exists()
    assert [d['erasure'] for _, d in erasures()] == ['record']


def test_a_withdrawal_recorded_by_staff_is_logged_too(member):
    staff = Identity.objects.create_user(email='support@example.test', is_staff=True)

    svc.record_consent(member['person'], 'my_record', ConsentEvent.WITHDRAWN, source=ConsentEvent.STAFF,
                       recorded_by=staff)

    assert not SurveyResponse.objects.filter(participant_id=member['person'].person_id).exists()
    assert erasures() == [(str(staff.pk), {'erasure': 'record', 'person_id': member['person'].person_id,
                                           'purpose': 'my_record'})]


@pytest.mark.parametrize('how', ['close', 'withdraw'])
def test_uploaded_documents_files_are_deleted_once_the_erasure_commits(member, how, settings, tmp_path,
                                                                        django_capture_on_commit_callbacks):
    settings.MEDIA_ROOT = str(tmp_path)
    doc = PatientDocument(person=member['person'], doc_type='FISH')
    doc.file.save('report.pdf', ContentFile(b'%PDF-1.4 the report'), save=True)
    stored = tmp_path / doc.file.name
    assert stored.exists()

    with django_capture_on_commit_callbacks(execute=True):
        if how == 'close':
            r = member['client'].delete('/api/v1/patient-records/me/', {'confirm': 'DELETE'}, format='json')
        else:
            r = withdraw(member['client'], 'my_record')

    assert r.status_code == 200
    assert not PatientDocument.objects.filter(pk=doc.pk).exists()
    assert not stored.exists()
