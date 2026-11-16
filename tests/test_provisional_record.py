"""The provisional pass, the PATCH mirror and the endpoint (phase 1c, #2531).

patient_record never holds a provisional value. Beside it, the provisional
table holds the best-known picture and the confidence table says which values
in it are proposals. Real beats provisional, a user's edit is real, and a
revert or an approval changes the picture on the next derivation.
"""
from datetime import date

import pytest
from rest_framework.test import APIClient

from omop_core.models import (
    PatientRecord, PatientRecordConfidence, PatientRecordProvisional,
)
from omop_core.services import provisional_record
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.signals import suppress_patient_record_refresh
from patient_portal.models import Identity, PatientUser
from tests.factories import MeasurementFactory, PatientRecordFactory
from tests.test_provisional_derivation import (  # noqa: F401 (fixtures)
    _add_measurement, _add_observation, _concept, record, snomed,
)

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def provisional_on(settings):
    settings.PROVISIONAL_DATA_ENABLED = True


def provisional(person):
    return PatientRecordProvisional.objects.get(person=person)


def confidence(person):
    return PatientRecordConfidence.objects.get(person=person)


def hemoglobin(person, value, when, conf=None):
    concept = _concept('Measurement', 3000963, '718-7', 'Hemoglobin [Mass/volume] in Blood', 'LOINC')
    return MeasurementFactory(
        person=person, measurement_concept=concept, measurement_date=when,
        value_as_number=value, measurement_source_value='718-7', unit_source_value='g/dL',
        value_source_value=str(value), confidence=conf)


# --- the pass --------------------------------------------------------------------------

def test_a_provisional_value_fills_an_empty_field_with_its_confidence(snomed, record):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)

    refreshed = refresh_patient_record(record.person)

    assert refreshed.hemoglobin_g_dl is None             # patient_record stays approved-only
    assert float(provisional(record.person).hemoglobin_g_dl) == 11.5
    assert confidence(record.person).hemoglobin_g_dl == 82


def test_the_provisional_row_is_the_whole_record_not_just_the_proposals(snomed, record):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)

    refreshed = refresh_patient_record(record.person)

    row = provisional(record.person)
    assert row.disease == refreshed.disease and row.patient_age == refreshed.patient_age
    assert row.organization_id == refreshed.organization_id


def test_real_beats_provisional(snomed, record):
    with suppress_patient_record_refresh():
        hemoglobin(record.person, 9.0, date(2024, 5, 1))
        hemoglobin(record.person, 11.5, date(2024, 6, 1), conf=82)   # newer, but a guess

    refreshed = refresh_patient_record(record.person)

    assert float(refreshed.hemoglobin_g_dl) == 9.0
    assert float(provisional(record.person).hemoglobin_g_dl) == 9.0
    assert confidence(record.person).hemoglobin_g_dl is None


def test_a_users_edit_wins_and_a_pending_clear_stays_clear(snomed, record):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
    PatientRecord.objects.filter(person=record.person).update(
        hemoglobin_g_dl=8, user_edited_fields=['hemoglobin_g_dl'])

    refresh_patient_record(record.person)
    assert float(provisional(record.person).hemoglobin_g_dl) == 8
    assert confidence(record.person).hemoglobin_g_dl is None

    # An explicit clear is an edit too: the proposal must not refill it.
    PatientRecord.objects.filter(person=record.person).update(
        hemoglobin_g_dl=None, user_edited_fields=['hemoglobin_g_dl'])
    refresh_patient_record(record.person)
    assert provisional(record.person).hemoglobin_g_dl is None
    assert confidence(record.person).hemoglobin_g_dl is None


def test_approval_makes_the_value_real_and_a_revert_removes_it(snomed, record):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
    refresh_patient_record(record.person)
    from omop_core.models import Measurement

    # Reverted: the row goes back to unresolved, no confidence. (A hospital's
    # own code, so the source value alone names no lab.)
    Measurement.objects.filter(person=record.person).update(
        measurement_concept_id=0, confidence=None, measurement_source_value='LOCAL-HGB')
    refresh_patient_record(record.person)
    assert provisional(record.person).hemoglobin_g_dl is None
    assert confidence(record.person).hemoglobin_g_dl is None

    # Approved: the same row is real, and so is the value.
    concept = _concept('Measurement', 3000963, '718-7', 'Hemoglobin [Mass/volume] in Blood', 'LOINC')
    Measurement.objects.filter(person=record.person).update(
        measurement_concept_id=concept.pk, confidence=None)   # still the hospital's code
    refreshed = refresh_patient_record(record.person)
    assert float(refreshed.hemoglobin_g_dl) == 11.5
    assert float(provisional(record.person).hemoglobin_g_dl) == 11.5
    assert confidence(record.person).hemoglobin_g_dl is None


def test_each_field_carries_the_confidence_of_the_code_that_decides_it(snomed, record):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
        _add_observation(record.person, 91)

    refresh_patient_record(record.person)

    marks = confidence(record.person)
    assert (marks.hemoglobin_g_dl, marks.b_symptom_fever) == (82, 91)


def test_too_many_code_groups_fall_back_to_the_lowest_confidence(snomed, record, monkeypatch):
    monkeypatch.setattr(provisional_record, 'MAX_ATTRIBUTION_GROUPS', 0)
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
        _add_observation(record.person, 91)

    refresh_patient_record(record.person)

    marks = confidence(record.person)
    assert (marks.hemoglobin_g_dl, marks.b_symptom_fever) == (82, 82)


def test_a_person_with_no_provisional_rows_gets_a_copy_of_the_record(snomed, record):
    refreshed = refresh_patient_record(record.person)

    row = provisional(record.person)
    assert row.disease == refreshed.disease
    marks = confidence(record.person)
    assert all(getattr(marks, f.name) is None
               for f in PatientRecordConfidence._meta.concrete_fields if f.name != 'person')


def test_the_switch_off_writes_nothing(snomed, record, settings):
    settings.PROVISIONAL_DATA_ENABLED = False
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)

    refresh_patient_record(record.person)

    assert not PatientRecordProvisional.objects.exists()
    assert not PatientRecordConfidence.objects.exists()


def test_a_failing_provisional_pass_does_not_lose_the_real_refresh(snomed, record, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError('provisional boom')
    monkeypatch.setattr(provisional_record, 'provisional_values', boom)
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)

    refreshed = refresh_patient_record(record.person)

    assert refreshed.derivation_version  # the approved record saved
    assert not PatientRecordProvisional.objects.exists()


# --- the PATCH mirror -----------------------------------------------------------------

@pytest.fixture()
def staff():
    user = Identity.objects.create_user(email='prov-staff@example.test', is_staff=True)
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_an_edit_reaches_the_provisional_row_and_drops_its_confidence(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
        _add_observation(record.person, 91)
    refresh_patient_record(record.person)

    response = staff.patch(f'/api/v1/patient-records/{record.person_id}/',
                           {'hemoglobin_g_dl': '13.0'}, format='json')

    assert response.status_code == 200, response.content
    row = provisional(record.person)
    assert float(row.hemoglobin_g_dl) == 13.0
    marks = confidence(record.person)
    assert marks.hemoglobin_g_dl is None         # the edit is approved
    assert marks.b_symptom_fever == 91           # the other proposal stays a proposal


def test_an_edit_with_no_provisional_row_writes_none(snomed, record, staff):
    _concept('Type Concept', 32865, '32865', 'Patient self-reported', 'Type Concept')
    response = staff.patch(f'/api/v1/patient-records/{record.person_id}/',
                           {'hemoglobin_g_dl': '13.0'}, format='json')

    assert response.status_code == 200, response.content
    assert not PatientRecordProvisional.objects.exists()


# --- the endpoint ---------------------------------------------------------------------

def test_detail_serves_the_provisional_picture_with_confidence(snomed, record, staff):
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
    refresh_patient_record(record.person)

    body = staff.get(f'/api/v1/patient-records-provisional/{record.person_id}/').json()
    real = staff.get(f'/api/v1/patient-records/{record.person_id}/').json()['patient_info']

    assert float(body['hemoglobin_g_dl']) == 11.5
    # The value and its aliases (hemoglobin_level and its unit) are one proposal.
    assert body['confidence']['hemoglobin_g_dl'] == 82
    assert set(body['confidence'].values()) == {82}
    assert real['hemoglobin_g_dl'] is None
    assert set(real) <= set(body)                 # the same fields, plus confidence


def test_a_person_not_yet_derived_gets_the_real_record_and_no_confidence(snomed, record, staff):
    body = staff.get(f'/api/v1/patient-records-provisional/{record.person_id}/').json()

    assert body['confidence'] == {} and body['disease'] == record.disease


def test_list_serves_every_visible_record(snomed, record, staff):
    other = PatientRecordFactory()
    with suppress_patient_record_refresh():
        _add_measurement(record.person, 82)
    refresh_patient_record(record.person)

    body = staff.get('/api/v1/patient-records-provisional/').json()

    rows = {row['person_id']: row for row in body['results']}
    assert set(rows) == {record.person_id, other.person_id}
    assert rows[record.person_id]['confidence']['hemoglobin_g_dl'] == 82
    assert rows[other.person_id]['confidence'] == {}


def test_the_patient_role_is_refused(snomed, record):
    identity = Identity.objects.create_user(email='prov-patient@example.test')
    PatientUser.objects.create(identity=identity, person=record.person)
    client = APIClient()
    client.force_authenticate(identity)

    assert client.get(f'/api/v1/patient-records-provisional/{record.person_id}/').status_code == 403
    assert client.get('/api/v1/patient-records-provisional/').status_code == 403
    # Their own real record is unaffected.
    assert client.get(f'/api/v1/patient-records/{record.person_id}/').status_code == 200


def test_another_professionals_patient_is_not_found(snomed, record):
    stranger = Identity.objects.create_user(email='prov-stranger@example.test')
    client = APIClient()
    client.force_authenticate(stranger)

    assert client.get(f'/api/v1/patient-records-provisional/{record.person_id}/').status_code == 404


def test_the_endpoint_does_not_exist_with_the_switch_off(snomed, record, staff, settings):
    settings.PROVISIONAL_DATA_ENABLED = False

    assert staff.get(f'/api/v1/patient-records-provisional/{record.person_id}/').status_code == 404
    assert staff.get('/api/v1/patient-records-provisional/').status_code == 404


def test_the_endpoint_is_read_only(snomed, record, staff):
    url = f'/api/v1/patient-records-provisional/{record.person_id}/'

    assert staff.patch(url, {'disease': 'x'}, format='json').status_code == 405
    assert staff.delete(url).status_code == 405
