"""The test-done and medication answers are tri-state (#2759).

An unanswered field is Unknown, not the former model default. The shared form
must offer Yes / No / Unknown explicitly for these clinical questions.
"""
import pytest
from rest_framework.test import APIClient

from omop_core.services.patient_record_service import (
    recompute_patient_record_fields,
    refresh_patient_record,
)
from omop_core.services.patient_form import apply_patient_form_patch
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.models import Identity
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

FIELDS = (
    'pulmonary_function_test_result',
    'bone_imaging_result',
    'pregnancy_test_result',
    'no_concomitant_medication_status',
)


def test_a_new_record_has_no_test_done_answers():
    record = PatientRecordFactory()
    for field in FIELDS:
        assert getattr(record, field) is None, field


@pytest.mark.parametrize('field', FIELDS)
def test_false_survives_recompute_and_refresh(field):
    record = PatientRecordFactory(**{field: True})
    setattr(record, field, False)
    record.user_edited_fields = [field]
    record.save()

    recompute_patient_record_fields(record)
    record.refresh_from_db()
    assert getattr(record, field) is False

    refreshed = refresh_patient_record(record.person)
    assert getattr(refreshed, field) is False


@pytest.mark.parametrize('field', FIELDS)
def test_unknown_is_storable_and_survives_recompute(field):
    record = PatientRecordFactory(**{field: True})
    setattr(record, field, None)
    record.user_edited_fields = [field]
    record.save()

    recompute_patient_record_fields(record)
    record.refresh_from_db()
    assert getattr(record, field) is None

    refreshed = refresh_patient_record(record.person)
    assert getattr(refreshed, field) is None


@pytest.mark.parametrize('field', FIELDS)
def test_the_descriptor_offers_three_states(field):
    entry = build_writable_field_descriptor()[field]
    assert entry['kind'] == 'direct'
    assert entry['writable'] is True
    assert entry['value_kind'] == 'boolean'
    assert entry['tri_state'] is True


@pytest.mark.parametrize('field', FIELDS)
def test_the_shared_form_writes_false_and_clears_to_unknown(field):
    record = PatientRecordFactory(**{field: True})

    saved, result = apply_patient_form_patch(record.person, {field: False})
    assert field in result.applied
    assert getattr(saved, field) is False

    saved, result = apply_patient_form_patch(record.person, {field: None})
    assert field in result.applied
    assert getattr(saved, field) is None


@pytest.mark.parametrize('field', FIELDS)
def test_omitting_the_field_preserves_the_answer(field):
    record = PatientRecordFactory(**{field: False})

    apply_patient_form_patch(record.person, {})

    record.refresh_from_db()
    assert getattr(record, field) is False


def test_the_shared_form_api_clears_to_unknown():
    record = PatientRecordFactory(pulmonary_function_test_result=False)
    client = APIClient()
    client.force_authenticate(
        Identity.objects.create_user(email='tri-state@example.test', is_staff=True))

    response = client.patch(
        f'/api/v1/patient-records/{record.person_id}/',
        {'pulmonary_function_test_result': None},
        format='json',
        HTTP_X_PATIENT_FORM='1',
    )

    assert response.status_code == 200, response.data
    assert 'pulmonary_function_test_result' in response.data['write']['applied']
    record.refresh_from_db()
    assert record.pulmonary_function_test_result is None
