"""The patient's "Confirm my record" while proposals are on display (#2761).

The page lays a proposed value over a field the approved record leaves empty and
marks it. A patient who confirms their record says what they saw is accurate, so
while a marked field they can edit is on display the endpoint refuses (409), and
editing the field, which saves it as approved data, clears the block. List
fields and fields with no write path of their own do not block.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    GroupAccess, Organization, Person, PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
)
from omop_core.services import provisional_record
from patient_portal.models import Identity, PatientUser

pytestmark = pytest.mark.django_db

CONFIRM = '/api/patient-info/me/confirm/'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''


@pytest.fixture()
def patient():
    """A patient with an approved record that leaves the hemoglobin empty."""
    org = Organization.objects.create(name='Attest Prov Org', slug='attest-prov-org')
    person = Person.objects.create(person_id=88801)
    record = PatientRecord.objects.create(person=person, organization=org)
    identity = Identity.objects.create_user(email='patient@attest-prov.test', password='pw')
    GroupAccess.objects.create(identity=identity, org=org, role='patient')
    PatientUser.objects.create(identity=identity, person=person, is_active=True)
    client = APIClient()
    client.force_authenticate(identity)
    return client, person, record


def propose(person, **fields):
    """Store the proposals the sweep would: values in the provisional table, marks beside."""
    values = {name: value for name, (value, _mark) in fields.items()}
    marks = {name: mark for name, (_value, mark) in fields.items()}
    PatientRecordProvisional.objects.update_or_create(person=person, defaults=values)
    PatientRecordConfidence.objects.update_or_create(person=person, defaults=marks)


def stored(person):
    person.refresh_from_db()
    record = PatientRecord.objects.get(person=person)
    return record, person


def test_a_patient_with_no_proposals_confirms_as_before(patient):
    client, person, _ = patient

    response = client.post(CONFIRM)

    assert response.status_code == 200
    record, person = stored(person)
    assert record.validated and person.validated
    assert record.validated_by == 'patient@attest-prov.test'


def test_a_proposed_value_the_patient_can_edit_refuses_the_confirmation(patient):
    client, person, _ = patient
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82))

    response = client.post(CONFIRM)

    assert response.status_code == 409
    body = response.json()
    assert body['provisional_fields'] == ['hemoglobin_g_dl']
    assert body['count'] == 1
    assert '1 value on your record is still marked Provisional' in body['detail']
    record, person = stored(person)
    assert not record.validated and not person.validated
    assert record.validated_by is None and record.validation_date is None
    assert person.validated_by is None and person.validation_date is None


def test_the_count_and_names_cover_every_proposal_the_patient_can_edit(patient):
    client, person, _ = patient
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82), sodium_meq_l=(Decimal('139'), 90))

    body = client.post(CONFIRM).json()

    assert body['provisional_fields'] == ['hemoglobin_g_dl', 'sodium_meq_l']
    assert body['count'] == 2
    assert '2 values on your record are still marked Provisional' in body['detail']


def test_a_proposed_list_alone_does_not_block(patient):
    client, person, _ = patient
    propose(person, prior_procedures=([{'procedure': 'PET scan'}], 72))
    assert provisional_record.overlay(PatientRecord.objects.get(person=person))[1] == {
        'prior_procedures': 72}                         # the page does carry the list's mark

    response = client.post(CONFIRM)

    assert response.status_code == 200
    assert stored(person)[0].validated


def test_a_field_with_no_write_path_of_its_own_does_not_block(patient):
    # A calculated field, a mirror of another field and a unit picker follow the field
    # they come from: the patient clears them by editing that one.
    from omop_core.services.write_descriptor import build_writable_field_descriptor
    client, person, _ = patient
    descriptor = build_writable_field_descriptor()
    assert [descriptor[f]['writable'] for f in ('bmi', 'serum_sodium', 'hemoglobin_level_units')] == [False] * 3
    propose(person, bmi=(Decimal('24.1'), 80), serum_sodium=(Decimal('139'), 90),
            hemoglobin_level_units=('g/dL', 73))

    response = client.post(CONFIRM)

    assert response.status_code == 200


def test_a_field_the_descriptor_does_not_know_blocks(patient, monkeypatch):
    client, person, _ = patient
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82))
    monkeypatch.setattr(
        'omop_core.services.write_descriptor.build_writable_field_descriptor', lambda: {})

    assert client.post(CONFIRM).status_code == 409


def test_with_the_switch_off_nothing_blocks(patient, settings):
    client, person, _ = patient
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82))
    settings.PROVISIONAL_DATA_ENABLED = False

    response = client.post(CONFIRM)

    assert response.status_code == 200
    assert stored(person)[0].validated


def test_editing_the_marked_field_clears_the_block(patient):
    client, person, _ = patient
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82))
    assert client.post(CONFIRM).status_code == 409
    shown = client.get('/api/patient-info/me/').json()['patient_info']
    assert shown['provisional_fields'] == {'hemoglobin_g_dl': 82}

    # The patient confirms the proposed value by editing the field: it is saved
    # as approved data, and the page no longer marks it.
    patch = client.patch('/api/patient-info/me/', {'hemoglobin_g_dl': '11.5'}, format='json')
    assert patch.status_code == 200
    shown = client.get('/api/patient-info/me/').json()['patient_info']
    assert 'hemoglobin_g_dl' not in shown['provisional_fields']

    response = client.post(CONFIRM)

    assert response.status_code == 200
    assert stored(person)[0].validated


def test_a_proposal_that_arrives_after_a_confirmation_refuses_a_reconfirmation(patient):
    client, person, _ = patient
    assert client.post(CONFIRM).status_code == 200
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82))

    assert client.post(CONFIRM).status_code == 409
    assert stored(person)[0].validated                 # an earlier confirmation is not undone


def test_someone_who_is_not_a_patient_is_not_asked_about_proposals(patient):
    _, person, _ = patient
    propose(person, hemoglobin_g_dl=(Decimal('11.5'), 82))
    staff = APIClient()
    staff.force_authenticate(Identity.objects.create_user(
        email='staff@attest-prov.test', password='pw', is_staff=True))

    response = staff.post(CONFIRM)

    assert response.status_code != 409                  # refused as before, for another reason


def test_blockers_are_sorted_and_empty_without_a_pair(patient):
    _, person, record = patient
    assert provisional_record.attestation_blockers(record) == []
    propose(person, sodium_meq_l=(Decimal('139'), 90), hemoglobin_g_dl=(Decimal('11.5'), 82))

    assert provisional_record.attestation_blockers(record) == ['hemoglobin_g_dl', 'sodium_meq_l']
