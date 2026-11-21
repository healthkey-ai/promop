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
from tests.test_provisional_computed import _height, _vital, _weight
from tests.test_provisional_derivation import (  # noqa: F401 (fixtures)
    _add_measurement, _add_observation, _concept, record, snomed,
)

pytestmark = pytest.mark.django_db

CONFIRM = '/api/patient-info/me/confirm/'


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True
    settings.CELERY_BROKER_URL = ''


class Editor(APIClient):
    """The page: a signed-in person's session, whose autosave names a cleared field in
    ``rejected_proposals`` (``rejectedProposals`` in provisional.ts). ``bare`` sends the
    body as it is, a blank with no such declaration."""

    def patch(self, path, data=None, format=None, **extra):
        if isinstance(data, dict) and 'rejected_proposals' not in data:
            blanks = [field for field, value in data.items() if value is None or value == '']
            if blanks:
                data = {**data, 'rejected_proposals': blanks}
        return super().patch(path, data, format=format, **extra)

    def bare(self, path, data):
        return super().patch(path, data, format='json')


def signed_in(identity):
    client = Editor()
    client.force_login(identity)
    return client


@pytest.fixture()
def staff():
    return signed_in(Identity.objects.create_user(email='rej-staff@example.test', is_staff=True))


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
    stored_rejections = {'weight': {('measurement', 'W1', 1), ('measurement', 'W2', 1)}}
    assert held(stored_rejections, {'weight': {('measurement', 'W1', 1)}}) == {'weight', 'weight_units'}
    assert held(stored_rejections, {'weight': {('measurement', 'W1', 1), ('measurement', 'W2', 1)}}) == {
        'weight', 'weight_units'}
    assert held(stored_rejections, {'weight': {('measurement', 'W1', 2)}}) == set()   # re-pointed concept
    assert held(stored_rejections, {'weight': {('measurement', 'W3', 1)}}) == set()   # another code
    assert held(stored_rejections, {'height': {('measurement', 'W1', 1)}}) == set()  # no rejection for height
    assert held(stored_rejections, {}) == set()


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
    assert provisional_record.proposal_clears(
        stored(record.person), {'hemoglobin_g_dl': None}, ['hemoglobin_g_dl']) == []
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
    client = signed_in(identity)
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


# --- a checklist whose empty string is a recorded answer (GELF, FLIPI) ------------------

@pytest.fixture()
def gelf_patient(patient, monkeypatch):
    """The patient above, with a proposed GELF answer that the attestation counts.

    The GELF control bypasses ``Field`` and is not yet on the list of fields that block a
    confirmation, so the list is widened here the way the change that adds it will.
    """
    client, person = patient
    PatientRecordProvisional.objects.filter(person=person).update(gelf_criteria_options='large_mass')
    PatientRecordConfidence.objects.filter(person=person).update(gelf_criteria_options=77)
    from omop_core.services import patient_visible_fields
    widened = frozenset({*patient_visible_fields.names(), 'gelf_criteria_options'})
    monkeypatch.setattr(patient_visible_fields, 'names', lambda: widened)
    return client, person


def test_null_on_a_proposed_gelf_rejects_it_and_lifts_the_block(gelf_patient):
    client, person = gelf_patient
    assert 'gelf_criteria_options' in provisional_record.attestation_blockers(stored(person))

    response = client.patch(url(person), {'gelf_criteria_options': None, 'hemoglobin_g_dl': None},
                            format='json')                                  # "Mark not assessed"

    assert response.status_code == 200, response.content
    after = shown(client, person)
    assert after['gelf_criteria_options'] is None
    assert 'gelf_criteria_options' not in after['provisional_fields']
    record = stored(person)
    assert record.gelf_criteria_options is None and record.gelf_criteria_status is None   # not assessed
    assert 'gelf_criteria_options' not in (record.user_edited_fields or [])
    assert client.post(CONFIRM).status_code == 200


def test_an_empty_string_on_a_proposed_gelf_records_not_met_and_lifts_the_block(gelf_patient):
    client, person = gelf_patient

    response = client.patch(url(person), {'gelf_criteria_options': '', 'hemoglobin_g_dl': None},
                            format='json')                                  # "Record no GELF criteria"

    assert response.status_code == 200, response.content
    record = stored(person)
    assert record.gelf_criteria_options == '' and record.gelf_criteria_status == 'Not Met'
    assert 'gelf_criteria_options' not in shown(client, person)['provisional_fields']
    assert not ProposalRejection.objects.filter(field_name='gelf_criteria_options').exists()
    assert client.post(CONFIRM).status_code == 200


def test_an_untouched_gelf_proposal_is_not_rejected_by_a_save_of_another_field(gelf_patient):
    client, person = gelf_patient

    response = client.patch(url(person), {'blood_type': 'A+'}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert shown(client, person)['provisional_fields']['gelf_criteria_options'] == 77
    declared = ['gelf_criteria_options']
    assert provisional_record.proposal_clears(stored(person), {'gelf_criteria_options': ''}, declared) == []
    assert provisional_record.proposal_clears(stored(person), {'gelf_criteria_options': None}, declared) == declared
    # Every other text field still treats '' as a blank.
    assert provisional_record._is_rejection('smoking_status', '') is True
    assert provisional_record._is_rejection('flipi_score_options', '') is False


# --- who may reject, and what the client declared (#2671 review) -------------------------

def test_a_blank_the_client_did_not_declare_is_not_a_rejection(snomed, record, staff):
    # A stale page, or a client that sends a whole representation, carries blanks it never
    # chose: nothing names the proposal as the thing cleared, so nothing is rejected and a
    # blank is stored as it always was.
    propose_hemoglobin(record.person)

    response = staff.bare(url(record.person), {'hemoglobin_g_dl': None, 'smoking_status': ''})

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert stored(record.person).smoking_status == ''                 # the answer, as before
    assert shown(staff, record.person)['provisional_fields']['hemoglobin_g_dl'] == 82


def test_a_declared_field_with_a_value_is_an_approval_not_a_rejection(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    propose_hemoglobin(record.person)

    response = staff.bare(url(record.person), {'hemoglobin_g_dl': '13', 'rejected_proposals': ['hemoglobin_g_dl']})

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert float(stored(record.person).hemoglobin_g_dl) == 13


def test_rejected_proposals_must_be_a_list_of_field_names(snomed, record, staff):
    propose_hemoglobin(record.person)

    for bad in ('hemoglobin_g_dl', {'hemoglobin_g_dl': True}, [1]):
        response = staff.bare(url(record.person), {'hemoglobin_g_dl': None, 'rejected_proposals': bad})
        assert response.status_code == 400, bad
    assert not ProposalRejection.objects.exists()


def test_the_survey_path_stores_a_blank_as_it_always_did(snomed, record):
    # survey_record_mapping writes its answers through apply_patient_record_patch with no
    # allow_reject: neither a blank nor a declaration of one is a rejection there.
    from patient_portal.api.views import apply_patient_record_patch
    propose_hemoglobin(record.person)
    identity = Identity.objects.create_user(email='rej-survey@example.test')

    apply_patient_record_patch(
        record.person, stored(record.person),
        {'smoking_status': '', 'hemoglobin_g_dl': None, 'rejected_proposals': ['hemoglobin_g_dl', 'smoking_status']},
        actor=identity)

    assert not ProposalRejection.objects.exists()
    assert stored(record.person).smoking_status == ''
    assert PatientRecordConfidence.objects.get(person=record.person).hemoglobin_g_dl == 82


def test_a_machine_credential_sending_null_does_not_reject(snomed, record, settings):
    from patient_portal.api.permissions import SERVICE_TOKEN
    settings.SERVICE_AUTH_SCOPES = 'patient/*.read patient/*.write'
    propose_hemoglobin(record.person)
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='rej-etl@example.test', is_staff=True),
                              token=SERVICE_TOKEN)

    response = client.patch(url(record.person),
                            {'hemoglobin_g_dl': None, 'rejected_proposals': ['hemoglobin_g_dl']}, format='json')

    assert response.status_code == 200, response.content
    assert not ProposalRejection.objects.exists()
    assert PatientRecordConfidence.objects.get(person=record.person).hemoglobin_g_dl == 82


def test_the_view_gates_rejection_on_a_signed_in_person(monkeypatch):
    from rest_framework.authentication import SessionAuthentication, TokenAuthentication
    from patient_portal.api import views
    from patient_portal.api.permissions import SERVICE_TOKEN
    seen = []
    monkeypatch.setattr(views, 'apply_patient_record_patch',
                        lambda *args, **kwargs: seen.append(kwargs['allow_reject']) or (None, {}))
    monkeypatch.setattr(views, 'PatientRecordSerializer', lambda record: type('S', (), {'data': {}})())
    viewset = views.PatientRecordViewSet()
    human = Identity(email='gate@example.test')

    def request(auth, authenticator):
        return type('R', (), {'data': {}, 'user': human, 'auth': auth, 'successful_authenticator': authenticator})()

    from oauth2_provider.models import Application
    from patient_portal.api.providers.base import TokenClaims

    def oauth(grant):
        application = type('A', (), {'authorization_grant_type': grant})()
        return type('T', (), {'application': application})()

    viewset._patch_record(request(None, SessionAuthentication()), None, None)
    assert seen[-1] is True                                      # a person at the editor
    partner = TokenClaims(issuer='firebase', sub='abc', email='gate@example.test', name=None, raw={})
    viewset._patch_record(request(partner, None), None, None)
    assert seen[-1] is True                                      # a partner (host app) login
    viewset._patch_record(request(SERVICE_TOKEN, None), None, None)
    assert seen[-1] is False                                     # a service credential
    viewset._patch_record(request(oauth(Application.GRANT_CLIENT_CREDENTIALS), None), None, None)
    assert seen[-1] is False                                     # a client-credentials app (an ETL)
    viewset._patch_record(request(oauth(Application.GRANT_AUTHORIZATION_CODE), None), None, None)
    assert seen[-1] is False                                     # a token a person handed to an app
    viewset._patch_record(request(object(), TokenAuthentication()), None, None)
    assert seen[-1] is False


def test_a_partner_login_can_reject_through_the_view(snomed, record):
    from patient_portal.api.providers.base import TokenClaims
    propose_hemoglobin(record.person)
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='rej-partner@example.test', is_staff=True),
        token=TokenClaims(issuer='firebase', sub='abc', email='rej-partner@example.test', name=None, raw={}))

    response = client.patch(url(record.person),
                            {'hemoglobin_g_dl': None, 'rejected_proposals': ['hemoglobin_g_dl']}, format='json')

    assert response.status_code == 200, response.content
    assert ProposalRejection.objects.filter(person=record.person, field_name='hemoglobin_g_dl').exists()


def test_a_verified_representative_can_reject_and_it_is_attributed(patient):
    from omop_core.models import PersonalRepresentative
    _, person = patient
    caregiver = Identity.objects.create_user(email='rej-caregiver@example.test')
    PersonalRepresentative.objects.create(
        representative=caregiver, person_id=person.person_id, relationship='caregiver',
        verification_status='VERIFIED')

    response = signed_in(caregiver).patch(url(person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 200, response.content
    assert ProposalRejection.objects.get(person=person).rejected_by == str(caregiver.pk)


def test_a_doctor_rejecting_for_a_patient_leaves_a_trace(patient):
    _, person = patient
    org = stored(person).organization
    doctor = Identity.objects.create_user(email='rej-doctor@example.test', password='pw')
    GroupAccess.objects.create(identity=doctor, org=org, role='doctor')

    response = signed_in(doctor).patch(url(person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 200, response.content
    rejection = ProposalRejection.objects.get(person=person, field_name='hemoglobin_g_dl')
    assert rejection.rejected_by == str(doctor.pk) and rejection.rejected_at is not None


def test_the_patients_own_rejection_names_the_patient(patient):
    client, person = patient

    client.patch(url(person), {'hemoglobin_g_dl': None}, format='json')

    own = PatientUser.objects.get(person=person).identity
    assert ProposalRejection.objects.get(person=person).rejected_by == str(own.pk)


def test_a_patient_whose_permission_is_withdrawn_cannot_reject_through_me_either(patient, monkeypatch):
    # /me/ has no can_write_patient gate in front of it: the refusal there is the record-off
    # rule in apply_patient_record_patch, which sees a blank for a proposal as the change it is.
    from patient_portal.consent import RECORD_OFF
    client, person = patient
    monkeypatch.setattr('patient_portal.consent.record_permitted', lambda person: False)

    response = client.patch('/api/v1/patient-records/me/', {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code == 403
    assert RECORD_OFF in str(response.json())
    assert not ProposalRejection.objects.exists()


def test_an_analyst_is_refused_and_rejects_nothing(patient):
    _, person = patient
    analyst = Identity.objects.create_user(email='rej-analyst@example.test', password='pw')
    GroupAccess.objects.create(identity=analyst, org=stored(person).organization, role='analyst')

    response = signed_in(analyst).patch(url(person), {'hemoglobin_g_dl': None}, format='json')

    assert response.status_code in (403, 404)
    assert not ProposalRejection.objects.exists()
    assert PatientRecordConfidence.objects.get(person=person).hemoglobin_g_dl == 82


def test_a_patch_with_nothing_blank_reads_no_proposals(snomed, record, staff, monkeypatch):
    # The rejection check must cost an ordinary save nothing: no overlay (and so no read of
    # the provisional tables) unless a declared field is blank.
    propose_hemoglobin(record.person)
    calls = []
    real = provisional_record.overlay
    monkeypatch.setattr(provisional_record, 'overlay', lambda *a, **k: calls.append(1) or real(*a, **k))

    staff.patch(url(record.person), {'blood_type': 'A+'}, format='json')
    staff.bare(url(record.person), {'sodium_meq_l': None})                   # blank, not declared
    staff.bare(url(record.person), {'sodium_meq_l': 140, 'rejected_proposals': ['sodium_meq_l']})
    assert calls == []

    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')
    assert calls                                                              # a declared blank reads them


# --- the rows a rejection is held against ----------------------------------------------

def test_an_unrelated_proposal_does_not_lapse_a_rejection(snomed, record, staff):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)                                  # hemoglobin, code 718-7
        _weight(record.person, 70, conf=75)                                  # an unrelated analyte
    refresh_patient_record(record.person)

    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')
    rejection = ProposalRejection.objects.get(person=record.person, field_name='hemoglobin_g_dl')
    assert rejection.origins == [['measurement', '718-7', 3000963]]          # not the weight's rows

    with suppress_patient_record_refresh():
        _height(record.person, 170, conf=60)                                 # a new, unrelated code
        _weight(record.person, 72, conf=75)
    refresh_patient_record(record.person)

    after = shown(staff, record.person)
    assert after['hemoglobin_g_dl'] is None
    assert after['provisional_fields']['weight'] == 75                       # the other proposals stand


def test_codes_that_agree_are_the_feeders_and_an_unrelated_code_is_not(snomed, record, staff):
    with suppress_patient_record_refresh():
        hemoglobin(record.person, 11.5, date(2024, 6, 1), source='HGB-A')
        hemoglobin(record.person, 11.5, date(2024, 6, 1), source='HGB-B')   # a second code, same answer
        _weight(record.person, 70, conf=75)
    refresh_patient_record(record.person)

    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')

    rejection = ProposalRejection.objects.get(person=record.person, field_name='hemoglobin_g_dl')
    assert rejection.origins == [['measurement', 'HGB-A', 3000963], ['measurement', 'HGB-B', 3000963]]
    with suppress_patient_record_refresh():
        _height(record.person, 170, conf=60)
    refresh_patient_record(record.person)
    assert shown(staff, record.person)['hemoglobin_g_dl'] is None


def test_too_many_code_groups_fall_back_to_every_row_and_can_only_lapse_more(snomed, record, staff, monkeypatch):
    monkeypatch.setattr(provisional_record, 'MAX_ORIGIN_GROUPS', 0)
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
        _weight(record.person, 70, conf=75)
    refresh_patient_record(record.person)

    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')

    origins = ProposalRejection.objects.get(person=record.person).origins
    assert len(origins) == 2                                                   # the weight's row too


def test_a_held_rejection_costs_nothing_when_every_row_is_one_it_was_made_against(snomed, record, monkeypatch):
    # The search for the rows that feed a field reads the rows per code group, on every
    # derivation while the hold applies; a person whose provisional rows are all the ones
    # the rejection was made against needs none of it.
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from omop_core.services.patient_record_service import _build_snapshot
    monkeypatch.setattr(provisional_record, 'MAX_ATTRIBUTION_GROUPS', 0)     # isolate the origin search
    counts = {}
    for extra in (2, 30):
        person = PatientRecordFactory().person
        with suppress_patient_record_refresh():
            hemoglobin(person, 11.5, date(2024, 6, 1))
            for number in range(extra):
                _vital(person, f'{1000 + number}-1', 70, 'kg', conf=75)
        origins = provisional_record._row_origins(
            list(Measurement.objects.filter(person=person, confidence__isnull=False)))
        ProposalRejection.objects.create(person=person, field_name='hemoglobin_g_dl',
                                         origins=[list(o) for o in sorted(origins)])
        searched = []
        real = provisional_record._field_origins
        monkeypatch.setattr(provisional_record, '_field_origins',
                            lambda *a, **k: searched.append(1) or real(*a, **k))
        with CaptureQueriesContext(connection) as queries:
            values, _ = provisional_record.provisional_values(
                person, PatientRecord.objects.get(person=person), _build_snapshot(person))
        counts[extra] = len(queries)
        assert 'hemoglobin_g_dl' not in values and searched == []
        monkeypatch.setattr(provisional_record, '_field_origins', real)
    assert counts[2] == counts[30]                                           # not per code group


def test_a_person_with_other_codes_pays_for_the_search_only_up_to_the_cap(snomed, record, monkeypatch):
    assert provisional_record.MAX_ORIGIN_GROUPS == 40                        # as the confidence's own cap
    reads = []
    real = provisional_record._read_values
    monkeypatch.setattr(provisional_record, '_read_values', lambda *a, **k: reads.append(1) or real(*a, **k))
    monkeypatch.setattr(provisional_record, 'MAX_ATTRIBUTION_GROUPS', 0)
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
        for number in range(5):
            _vital(record.person, f'{2000 + number}-1', 70, 'kg', conf=75)
    ProposalRejection.objects.create(person=record.person, field_name='hemoglobin_g_dl',
                                     origins=[['measurement', '718-7', 3000963]])
    from omop_core.services.patient_record_service import _build_snapshot
    run = lambda: provisional_record.provisional_values(
        record.person, stored(record.person), _build_snapshot(record.person))

    monkeypatch.setattr(provisional_record, 'MAX_ORIGIN_GROUPS', 3)          # six groups: over the cap
    run()
    over = len(reads)
    reads.clear()
    monkeypatch.setattr(provisional_record, 'MAX_ORIGIN_GROUPS', 40)         # within it: a read per group
    run()

    assert over == 1 and len(reads) > over


# --- a date and its precision are one answer -------------------------------------------

def propose_fields(person, **fields):
    values = {name: value for name, (value, _mark) in fields.items()}
    marks = {name: mark for name, (_value, mark) in fields.items()}
    PatientRecordProvisional.objects.update_or_create(person=person, defaults=values)
    PatientRecordConfidence.objects.update_or_create(person=person, defaults=marks)


def test_a_rejected_date_takes_its_precision_with_it():
    held = provisional_record.rejection_group
    assert held(['sct_date']) == {'sct_date', 'sct_date_precision'}
    assert held(['dlbcl_transformation_date']) == {
        'dlbcl_transformation_date', 'dlbcl_transformation_date_precision'}
    assert 'diagnosis_date_precision' in held(['diagnosis_date'])


def test_a_proposed_precision_is_not_shown_against_an_approved_date():
    record = PatientRecordFactory(diagnosis_date=date(2020, 5, 12))
    propose_fields(record.person, diagnosis_date_precision=('year', 80))

    view, marks = provisional_record.overlay(stored(record.person))

    assert marks == {} and view.diagnosis_date_precision is None
    assert provisional_record.attestation_blockers(stored(record.person)) == []


def test_a_proposed_precision_is_shown_with_its_proposed_date():
    record = PatientRecordFactory(diagnosis_date=None)
    propose_fields(record.person, diagnosis_date=(date(2020, 5, 12), 80), diagnosis_date_precision=('year', 80))

    view, marks = provisional_record.overlay(stored(record.person))

    assert marks == {'diagnosis_date': 80, 'diagnosis_date_precision': 80}
    assert view.diagnosis_date_precision == 'year'


def test_editing_a_date_takes_its_precision_proposal_back():
    record = PatientRecordFactory(diagnosis_date=None)
    propose_fields(record.person, diagnosis_date=(date(2020, 5, 12), 80), diagnosis_date_precision=('year', 80))
    PatientRecord.objects.filter(person=record.person).update(
        diagnosis_date=date(2021, 1, 1), user_edited_fields=['diagnosis_date'])

    provisional_record.mirror_user_edit(stored(record.person), ['diagnosis_date'])

    marks = PatientRecordConfidence.objects.get(person=record.person)
    assert marks.diagnosis_date is None and marks.diagnosis_date_precision is None
    provisional = PatientRecordProvisional.objects.get(person=record.person)
    assert provisional.diagnosis_date == date(2021, 1, 1) and provisional.diagnosis_date_precision is None
    assert provisional_record.overlay(stored(record.person))[1] == {}


# --- what travels and what is erased ---------------------------------------------------

def test_erasing_a_record_deletes_its_rejections(snomed, record, staff):
    from patient_portal.erasure import erase_record
    propose_hemoglobin(record.person)
    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')
    other = PatientRecordFactory().person
    ProposalRejection.objects.create(person=other, field_name='weight', origins=[])
    assert ProposalRejection.objects.filter(person=record.person).count() == 1

    erase_record(record.person)

    assert not ProposalRejection.objects.filter(person=record.person).exists()
    assert ProposalRejection.objects.filter(person=other).exists()           # nobody else's


def test_a_rejection_is_not_removed_by_approval_or_typing_and_is_then_ignored(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    propose_hemoglobin(record.person)
    staff.patch(url(record.person), {'hemoglobin_g_dl': None}, format='json')

    staff.patch(url(record.person), {'hemoglobin_g_dl': '13'}, format='json')

    assert ProposalRejection.objects.filter(person=record.person, field_name='hemoglobin_g_dl').exists()
    after = shown(staff, record.person)
    assert float(after['hemoglobin_g_dl']) == 13 and 'hemoglobin_g_dl' not in after['provisional_fields']
