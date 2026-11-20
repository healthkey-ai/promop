"""A user rejects a displayed proposal (#2671).

The record shows a proposed value on a field the approved record leaves empty. The
editor sends null (or an empty string) when the user clears it, which used to be a
no-op because the record already held nothing there, so the proposal came back at
the next derivation. A blank value for a field the record marks is now the user
rejecting that proposal: it is remembered per person and field
(``ProposalRejection``), the pass leaves the field empty while every row that decides
it is one that was rejected, and it deletes and approves nothing.
"""
from datetime import date

import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    GroupAccess, Measurement, Observation, Organization, PatientRecord, PatientRecordConfidence,
    PatientRecordProvisional, Person, ProposalRejection,
)
from omop_core.services import provisional_record
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import MeasurementFactory, PatientRecordFactory
from tests.test_provisional_composite import cancer
from tests.test_provisional_computed import _height, _weight
from tests.test_provisional_derivation import (  # noqa: F401 (fixtures)
    _add_measurement, _add_observation, _concept, record, snomed,
)

pytestmark = pytest.mark.django_db

CONFIRM = '/api/patient-info/me/confirm/'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''


@pytest.fixture()
def staff():
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='rej-staff@example.test', is_staff=True))
    return client


def url(person):
    return f'/api/v1/patient-records/{person.person_id}/'


def shown(client, person):
    return client.get(url(person)).json()['patient_info']


def stored(person):
    return PatientRecord.objects.get(person=person)


def pair(person):
    return (PatientRecordProvisional.objects.get(person=person),
            PatientRecordConfidence.objects.get(person=person))


def hemoglobin(person, value, when, source='718-7'):
    concept = _concept('Measurement', 3000963, '718-7', 'Hemoglobin [Mass/volume] in Blood', 'LOINC')
    return MeasurementFactory(
        person=person, measurement_concept=concept, measurement_date=when, value_as_number=value,
        measurement_source_value=source, unit_source_value='g/dL', value_source_value=str(value),
        confidence=82)


def propose_hemoglobin(person):
    with suppress_patient_record_refresh():
        _add_measurement(person, 82)
    refresh_patient_record(person)


# --- rejecting a displayed proposal ----------------------------------------------------

def test_clearing_a_displayed_proposal_rejects_it_and_it_stays_rejected(snomed, record, staff):
    propose_hemoglobin(record.person)
    assert shown(staff, record.person)['provisional_fields']['hemoglobin_g_dl'] == 82

    response = staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 200, response.content
    after = shown(staff, record.person)
    assert after['hemoglobin_g_dl'] is None
    assert 'hemoglobin_g_dl' not in after['provisional_fields']
    assert not any(name.startswith('hemoglobin') for name in after['provisional_fields'])   # the aliases go too
    row, marks = pair(record.person)
    assert row.hemoglobin_g_dl is None and marks.hemoglobin_g_dl is None
    rejection = ProposalRejection.objects.get(person=record.person, field_name='hemoglobin_g_dl')
    assert rejection.origins == [['measurement', '718-7', 3000963]]
    # Nothing was approved or deleted: the record and the clinical row are as they were.
    assert stored(record.person).hemoglobin_g_dl is None
    assert 'hemoglobin_g_dl' not in (stored(record.person).user_edited_fields or [])
    assert Measurement.objects.filter(person=record.person, confidence=82).count() == 1

    # A later derivation does not bring it back: the rejection is what the pass reads.
    refresh_patient_record(record.person)
    assert shown(staff, record.person)['hemoglobin_g_dl'] is None
    assert not PatientRecordConfidence.objects.get(person=record.person).hemoglobin_g_dl


def test_an_empty_string_rejects_a_proposal_on_a_text_field_too(snomed, person_with_disease_proposal, staff):
    person = person_with_disease_proposal
    assert shown(staff, person)['provisional_fields']['diagnosis_date_precision'] == 82

    response = staff.patch(url(person), {'diagnosis_date_precision': ''}, format='json')

    assert response.status_code == 200, response.content
    after = shown(staff, person)
    assert 'diagnosis_date_precision' not in after['provisional_fields']
    assert after['diagnosis_date_precision'] in (None, '')
    assert stored(person).diagnosis_date_precision in (None, '')       # no '' stored as an edit
    assert 'diagnosis_date_precision' not in (stored(person).user_edited_fields or [])
    assert ProposalRejection.objects.get(person=person).field_name == 'diagnosis_date_precision'
    # Only the field that was rejected: the rest of the group is still a proposal.
    assert after['provisional_fields'].get('diagnosis_date') == 82


def test_a_new_reading_of_the_rejected_code_stays_rejected(snomed, record, staff):
    propose_hemoglobin(record.person)
    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')

    with suppress_patient_record_refresh():
        hemoglobin(record.person, 9.1, date(2024, 9, 1))
    refresh_patient_record(record.person)

    assert shown(staff, record.person)['hemoglobin_g_dl'] is None


def test_a_different_code_feeding_the_field_is_a_new_proposal_and_shows_again(snomed, record, staff):
    propose_hemoglobin(record.person)
    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')
    assert ProposalRejection.objects.filter(person=record.person, field_name='hemoglobin_g_dl').exists()

    with suppress_patient_record_refresh():
        hemoglobin(record.person, 9.1, date(2024, 9, 1), source='HGB-NEW')
    refresh_patient_record(record.person)

    after = shown(staff, record.person)
    assert float(after['hemoglobin_g_dl']) == 9.1
    assert after['provisional_fields']['hemoglobin_g_dl'] == 82


def test_the_hold_is_every_deciding_row_being_one_that_was_rejected():
    person = PatientRecordFactory().person
    ProposalRejection.objects.create(person=person, field_name='weight', origins=[
        ['measurement', 'W1', 1], ['measurement', 'W2', 1]])
    held = provisional_record.held_rejections
    assert held(person, {'weight': {('measurement', 'W1', 1)}}) == {'weight', 'weight_units'}
    assert held(person, {'weight': {('measurement', 'W1', 1), ('measurement', 'W2', 1)}}) == {
        'weight', 'weight_units'}
    assert held(person, {'weight': {('measurement', 'W1', 2)}}) == set()          # re-pointed concept
    assert held(person, {'weight': {('measurement', 'W3', 1)}}) == set()          # another code
    assert held(person, {'height': {('measurement', 'W1', 1)}}) == set()          # no rejection for height
    assert held(person, {}) == set()


# --- what does not count as a rejection ------------------------------------------------

def test_an_edit_of_another_field_does_not_reject_a_displayed_proposal(snomed, record, staff):
    propose_hemoglobin(record.person)

    response = staff.patch(url(record.person), {'blood_type': 'A+'}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert shown(staff, record.person)['provisional_fields']['hemoglobin_g_dl'] == 82


def test_a_blank_value_for_a_field_with_no_proposal_is_not_a_rejection(snomed, record, staff):
    propose_hemoglobin(record.person)

    response = staff.patch(url(record.person), {'sodium_meq_l': None, 'blood_type': ''}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()


def test_clearing_a_real_value_is_the_ordinary_clear_not_a_rejection(snomed, record, staff):
    PatientRecord.objects.filter(person=record.person).update(blood_type='A+')
    propose_hemoglobin(record.person)

    response = staff.patch(url(record.person), {'blood_type': None}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert stored(record.person).blood_type is None


def test_typing_the_value_back_is_an_approval_not_a_rejection(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    propose_hemoglobin(record.person)

    response = staff.patch(url(record.person), {'hemoglobin_g_dl': '11.5'}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert float(stored(record.person).hemoglobin_g_dl) == 11.5


def test_a_calculated_field_and_a_list_field_are_not_rejected_by_a_clear(snomed, record, staff):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    refresh_patient_record(record.person)
    assert shown(staff, record.person)['provisional_fields']['bmi'] == 75

    staff.patch(url(record.person), {'bmi': None, 'prior_procedures': []}, format='json')

    assert not ProposalRejection.objects.exists()
    assert shown(staff, record.person)['provisional_fields']['bmi'] == 75


def test_with_the_switch_off_a_blank_value_changes_nothing(snomed, record, staff, settings):
    propose_hemoglobin(record.person)
    settings.PROVISIONAL_DATA_ENABLED = False

    response = staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert provisional_record.proposal_clears(stored(record.person), {'hemoglobin_g_dl': None}) == []
    settings.PROVISIONAL_DATA_ENABLED = True
    assert shown(staff, record.person)['provisional_fields']['hemoglobin_g_dl'] == 82    # untouched


# --- the fields that go with the rejected one ------------------------------------------

def test_rejecting_a_weight_takes_its_unit_and_the_bmi_made_from_it(snomed, record, staff):
    with suppress_patient_record_refresh():
        _weight(record.person, 70, conf=82)
        _height(record.person, 170, conf=75)
    refresh_patient_record(record.person)
    marks = shown(staff, record.person)['provisional_fields']
    assert {'weight', 'height', 'bmi'} <= set(marks)

    response = staff.patch(url(record.person), {'weight': None}, format='json')

    assert response.status_code == 200, response.content
    after = shown(staff, record.person)['provisional_fields']
    assert 'weight' not in after and 'weight_units' not in after and 'bmi' not in after
    assert after['height'] == 75                                  # the height is still a proposal
    refresh_patient_record(record.person)                         # and the pass agrees
    again = shown(staff, record.person)['provisional_fields']
    assert 'weight' not in again and 'bmi' not in again and again['height'] == 75


def test_rejecting_the_disease_rejects_the_whole_group(snomed, staff):
    person = PatientRecordFactory().person
    with suppress_patient_record_refresh():
        cancer(person, 82)
    refresh_patient_record(person)
    assert {'disease', 'diagnosis_date'} <= set(shown(staff, person)['provisional_fields'])

    response = staff.patch(url(person), {'disease': None}, format='json')

    assert response.status_code == 200, response.content
    after = shown(staff, person)
    for field in provisional_record.DISEASE_FIELDS:
        assert field not in after['provisional_fields'], field
    assert after['disease'] is None and after['diagnosis_date'] is None
    refresh_patient_record(person)
    assert not set(provisional_record.DISEASE_FIELDS) & set(shown(staff, person)['provisional_fields'])


@pytest.fixture()
def person_with_disease_proposal(snomed):
    person = PatientRecordFactory().person
    with suppress_patient_record_refresh():
        cancer(person, 82)
    refresh_patient_record(person)
    return person


# --- the patient's own record ----------------------------------------------------------

@pytest.fixture()
def patient(snomed):
    """A patient (with a proposal on their record) and the client that is them."""
    org = Organization.objects.create(name='Reject Org', slug='reject-org')
    person = Person.objects.create(person_id=88811)
    PatientRecord.objects.create(person=person, organization=org)
    identity = Identity.objects.create_user(email='rej-patient@example.test', password='pw')
    GroupAccess.objects.create(identity=identity, org=org, role='patient')
    PatientUser.objects.create(identity=identity, person=person, is_active=True)
    client = APIClient()
    client.force_authenticate(identity)
    propose_hemoglobin(person)
    return client, person


def test_a_patient_rejects_a_proposal_and_can_then_confirm_their_record(patient):
    client, person = patient
    assert client.post(CONFIRM).status_code == 409                  # the proposal is on display

    response = client.patch(url(person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 200, response.content
    assert 'hemoglobin_g_dl' not in shown(client, person)['provisional_fields']
    assert provisional_record.attestation_blockers(stored(person)) == []
    assert client.post(CONFIRM).status_code == 200


def test_a_patients_rejection_reaches_only_their_own_record(patient):
    client, person = patient
    other = PatientRecordFactory().person
    propose_hemoglobin(other)

    client.patch(url(person), {'hemoglobin_g_dl': None}, format='json')
    refused = client.patch(url(other), {'hemoglobin_g_dl': None}, format='json')

    assert refused.status_code in (403, 404)
    assert list(ProposalRejection.objects.values_list('person_id', flat=True)) == [person.person_id]
    assert PatientRecordConfidence.objects.get(person=other).hemoglobin_g_dl == 82


def test_a_patient_whose_record_permission_is_withdrawn_cannot_reject(patient, monkeypatch):
    client, person = patient
    monkeypatch.setattr('patient_portal.consent.record_permitted', lambda person: False)

    response = client.patch(url(person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 403
    assert not ProposalRejection.objects.exists()
    assert PatientRecordConfidence.objects.get(person=person).hemoglobin_g_dl == 82


def test_a_rejection_travels_with_the_patient_and_is_erased_with_them():
    from omop_core.services import instance_data
    from omop_core.services.patient_transfer import PATIENT_TABLES
    assert 'omop_core.ProposalRejection' in instance_data.PATIENT
    assert ProposalRejection in {table.model for table in PATIENT_TABLES}
    from patient_portal.erasure import withdrawal_tables
    assert ProposalRejection in {model for model, _ in withdrawal_tables()}
