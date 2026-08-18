import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponseRedirect
from rest_framework.test import APIRequestFactory

from omop_core.models import ConditionOccurrence, PatientRecord, Person, ProvenanceRecord
from patient_portal.api.views import PatientRecordViewSet
from patient_portal.models import Identity, PatientUser
from patient_portal import views as portal_views


pytestmark = pytest.mark.django_db


def test_csv_writes_dated_disease_to_omop_then_derives_patient_record():
    from omop_core.test_utils import ensure_test_concept_zero
    from patient_portal.tests import _make_vocab_fixtures

    ensure_test_concept_zero()
    _make_vocab_fixtures()
    request = APIRequestFactory().post(
        '/api/patient-info/upload_csv/',
        {
            'file': SimpleUploadedFile(
                'patients.csv',
                b'person_id,disease,date_of_birth,diagnosis_date,phone_number\n98724,Breast Cancer,1980-01-02,2020-04-03,555-0100\n',
                content_type='text/csv',
            ),
        },
        format='multipart',
    )

    response = PatientRecordViewSet().upload_csv(request)

    assert response.status_code == 200
    assert response.data['errors'] == []
    person = Person.objects.get(person_id=98724)
    assert person.year_of_birth == 1980
    assert person.phone_number == '555-0100'
    condition = ConditionOccurrence.objects.get(person=person)
    assert condition.condition_source_value == 'Breast Cancer'
    assert str(condition.condition_start_date) == '2020-04-03'
    assert ProvenanceRecord.objects.filter(object_id=condition.pk).exists()
    record = PatientRecord.objects.get(person=person)
    assert record.disease == 'Breast Cancer'
    assert str(record.date_of_birth) == '1980-01-02'


def test_csv_rejects_undated_disease_without_creating_a_person():
    request = APIRequestFactory().post(
        '/api/patient-info/upload_csv/',
        {'file': SimpleUploadedFile('patients.csv', b'person_id,disease\n98723,Breast Cancer\n', content_type='text/csv')},
        format='multipart',
    )

    response = PatientRecordViewSet().upload_csv(request)

    assert response.status_code == 200
    assert 'disease requires diagnosis_date' in response.data['errors'][0]
    assert not Person.objects.filter(person_id=98723).exists()


def test_server_rendered_mapped_submission_does_not_persist_projection_value(monkeypatch):
    identity = Identity.objects.create_user(email='legacy-form@example.test', password='pw')
    person = Person.objects.create(person_id=98722)
    PatientUser.objects.create(identity=identity, person=person)
    record = PatientRecord.objects.create(person=person, disease='Breast Cancer')
    request = RequestFactory().post(
        '/health-records/update/', {'tab': 'general', 'disease': 'Lung Cancer'},
    )
    SessionMiddleware(lambda value: None).process_request(request)
    request.session.save()
    MessageMiddleware(lambda value: None).process_request(request)
    request.user = identity
    monkeypatch.setattr(portal_views, 'redirect', lambda *args, **kwargs: HttpResponseRedirect('/health-records/'))
    response = portal_views.update_health_records(request)

    assert response.status_code == 302
    record.refresh_from_db()
    assert record.disease == 'Breast Cancer'
