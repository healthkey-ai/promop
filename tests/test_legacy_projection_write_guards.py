import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpResponseRedirect
from rest_framework.test import APIRequestFactory

from omop_core.models import PatientRecord, Person
from patient_portal.api.views import PatientRecordViewSet
from patient_portal.models import Identity, PatientUser
from patient_portal import views as portal_views


pytestmark = pytest.mark.django_db


def test_csv_mapped_columns_are_rejected_before_person_or_projection_creation():
    request = APIRequestFactory().post(
        '/api/patient-info/upload_csv/',
        {
            'file': SimpleUploadedFile(
                'patients.csv',
                b'person_id,disease,date_of_birth\n98721,Breast Cancer,1980-01-02\n',
                content_type='text/csv',
            ),
        },
        format='multipart',
    )

    response = PatientRecordViewSet().upload_csv(request)

    assert response.status_code == 200
    assert response.data['created_count'] == 0
    assert 'mapped clinical columns' in response.data['errors'][0]
    assert not Person.objects.filter(person_id=98721).exists()
    assert not PatientRecord.objects.filter(person_id=98721).exists()


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
