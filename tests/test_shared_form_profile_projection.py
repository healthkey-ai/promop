"""Shared form writes profile values to PatientRecord before Person/Location.

Profile edits are part of the same PatientRecord PATCH as clinical edits: the
record value is saved first, then Person/Location is updated without a full
OMOP derivation (docs/patient-record-first-writes.md, Profile fields).
"""
from datetime import date

import pytest
from rest_framework.test import APIClient

from omop_core.models import Location, PatientRecord, Person
from omop_core.services.patient_record_service import refresh_patient_record
from patient_portal.models import Identity
from tests.factories import PatientRecordFactory, PersonFactory

pytestmark = pytest.mark.django_db


def _staff_client():
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='profile-form-staff@example.test', is_staff=True)
    )
    return client


def _patch(client, record, payload):
    return client.patch(
        f'/api/v1/patient-records/{record.person_id}/', payload,
        format='json', HTTP_X_PATIENT_FORM='1',
    )


def test_profile_edit_saves_patient_record_without_refresh(monkeypatch):
    record = PatientRecordFactory()
    calls = []
    monkeypatch.setattr(
        'omop_core.services.patient_form.refresh_patient_record',
        lambda person: calls.append(person.pk),
    )
    monkeypatch.setattr(
        'omop_core.services.field_write_service.refresh_patient_record',
        lambda person: calls.append(person.pk),
    )

    response = _patch(_staff_client(), record, {'gender': 'Female'})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.gender == 'F'
    person = Person.objects.get(pk=record.person_id)
    assert person.gender_source_value == 'Female'
    assert calls == []


def test_profile_location_edit_writes_location_without_refresh(monkeypatch):
    record = PatientRecordFactory()
    calls = []
    monkeypatch.setattr(
        'omop_core.services.patient_form.refresh_patient_record',
        lambda person: calls.append(person.pk),
    )
    monkeypatch.setattr(
        'omop_core.services.field_write_service.refresh_patient_record',
        lambda person: calls.append(person.pk),
    )

    response = _patch(_staff_client(), record, {'city': 'NYC', 'region': 'NY'})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.city == 'NYC'
    assert record.region == 'NY'
    person = Person.objects.get(pk=record.person_id)
    location = Location.objects.get(location_id=person.location_id)
    assert location.city == 'NYC'
    assert location.state == 'NY'
    assert calls == []


def test_unpaired_coordinate_returns_400_without_partial_write():
    record = PatientRecordFactory()

    response = _patch(_staff_client(), record, {'city': 'NYC', 'latitude': 40.7})

    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.city is None
    person = Person.objects.get(pk=record.person_id)
    assert person.location_id is None


def test_profile_clear_writes_none_without_refresh():
    record = PatientRecordFactory(city='NYC')
    person = Person.objects.get(pk=record.person_id)
    location = Location.objects.create(
        location_id=999001, city='NYC', state='NY',
        latitude=40.7, longitude=-74.0,
    )
    person.location_id = location.location_id
    person.save(update_fields=['location_id'])

    response = _patch(_staff_client(), record, {'city': ''})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.city is None
    location.refresh_from_db()
    assert location.city is None


def test_clearing_one_coordinate_when_record_has_neither_clears_location_pair():
    record = PatientRecordFactory(city='NYC')
    person = Person.objects.get(pk=record.person_id)
    location = Location.objects.create(
        location_id=999002, city='NYC', latitude=40.7, longitude=-74.0,
    )
    person.location_id = location.location_id
    person.save(update_fields=['location_id'])

    response = _patch(_staff_client(), record, {'latitude': ''})

    assert response.status_code == 200, response.data
    location.refresh_from_db()
    assert location.latitude is None
    assert location.longitude is None


def test_clearing_one_coordinate_when_record_has_pair_is_rejected():
    record = PatientRecordFactory(latitude=40.7, longitude=-74.0)

    response = _patch(_staff_client(), record, {'latitude': ''})

    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.latitude == 40.7
    assert record.longitude == -74.0


def test_out_of_range_latitude_returns_400_without_write():
    record = PatientRecordFactory()

    response = _patch(_staff_client(), record, {'latitude': 91, 'longitude': 0})

    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.latitude is None
    assert record.longitude is None


def test_partial_coordinate_is_rejected_when_record_has_neither():
    record = PatientRecordFactory()
    person = Person.objects.get(pk=record.person_id)
    location = Location.objects.create(
        location_id=999003, latitude=40.7, longitude=-74.0,
    )
    person.location_id = location.location_id
    person.save(update_fields=['location_id'])

    response = _patch(_staff_client(), record, {'latitude': 41.0})

    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.latitude is None


def test_profile_projection_failure_keeps_patient_record(monkeypatch):
    record = PatientRecordFactory()

    def boom(*_args, **_kwargs):
        raise RuntimeError('projection is down')

    monkeypatch.setattr('omop_core.services.patient_form.apply_field_writes', boom)

    response = _patch(_staff_client(), record, {'city': 'NYC'})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.city == 'NYC'
    assert 'city' in response.data['write']['applied']
    assert 'city' in (record.user_edited_fields or [])

    # A later derivation must not restore the old Location value over the
    # saved edit while the projection is still missing.
    from omop_core.services.patient_record_service import refresh_patient_record
    refreshed = refresh_patient_record(record.person)
    assert refreshed.city == 'NYC'


def test_whitespace_only_profile_value_is_a_clear():
    record = PatientRecordFactory(city='NYC')

    response = _patch(_staff_client(), record, {'city': '   '})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.city is None


def test_clearing_both_coordinates_is_allowed():
    record = PatientRecordFactory(latitude=40.7, longitude=-74.0)

    response = _patch(_staff_client(), record, {'latitude': '', 'longitude': ''})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.latitude is None
    assert record.longitude is None


def test_profile_record_save_precedes_person_save(monkeypatch):
    person = PersonFactory(year_of_birth=1980, month_of_birth=5, day_of_birth=6)
    record = PatientRecordFactory(person=person)
    order = []
    seen = {}
    original_record_save = PatientRecord.save
    original_person_save = Person.save

    def record_save(self, *args, **kwargs):
        order.append('record')
        return original_record_save(self, *args, **kwargs)

    def person_save(self, *args, **kwargs):
        order.append('person')
        seen['dob'] = PatientRecord.objects.get(pk=record.pk).date_of_birth
        return original_person_save(self, *args, **kwargs)

    monkeypatch.setattr(PatientRecord, 'save', record_save)
    monkeypatch.setattr(Person, 'save', person_save)

    response = _patch(_staff_client(), record, {'date_of_birth': '1980-05-06'})

    assert response.status_code == 200, response.data
    assert order.index('record') < order.index('person')
    assert seen['dob'] == date(1980, 5, 6)
    record.refresh_from_db()
    assert record.date_of_birth == date(1980, 5, 6)
    assert record.date_of_birth_precision == 'day'


def test_facility_name_projects_to_person(monkeypatch):
    record = PatientRecordFactory()
    calls = []
    monkeypatch.setattr(
        'omop_core.services.patient_form.refresh_patient_record',
        lambda person: calls.append(person.pk),
    )

    response = _patch(_staff_client(), record, {'facility_name': 'Clinic A'})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.facility_name == 'Clinic A'
    person = Person.objects.get(pk=record.person_id)
    assert person.facility_name == 'Clinic A'
    assert calls == []
    assert refresh_patient_record(record.person).facility_name == 'Clinic A'


def test_deferred_profile_field_stays_pending():
    record = PatientRecordFactory()

    response = _patch(_staff_client(), record, {'validated': True})

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert record.validated is True
    assert 'validated' in (record.user_edited_fields or [])
    assert refresh_patient_record(record.person).validated is True


def test_shared_form_patch_does_not_attach_latest_fact_dates(monkeypatch):
    record = PatientRecordFactory()
    calls = []
    monkeypatch.setattr(
        'omop_core.services.patient_form._attach_latest_fact_dates',
        lambda *args: calls.append(args),
    )

    response = _patch(_staff_client(), record, {'city': 'NYC'})

    assert response.status_code == 200, response.data
    assert calls == []
