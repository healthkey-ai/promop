"""Computed-only fields are refused, not silently dropped (#2135)."""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientRecord, Person
from omop_core.services.write_descriptor import (
    build_writable_field_descriptor, computed_only_fields,
    get_serializer_read_only_fields,
)
from patient_portal.models import Identity

pytestmark = pytest.mark.django_db

PERSON_ID = 99101
NATIVE = f'/api/patient-info/{PERSON_ID}/'
FORM = f'/api/v1/patient-records/{PERSON_ID}/'


@pytest.fixture
def client():
    api = APIClient()
    api.force_authenticate(Identity.objects.create_user(
        email='computed-only@example.test', is_staff=True))
    Person.objects.create(person_id=PERSON_ID)
    PatientRecord.objects.get_or_create(person_id=PERSON_ID)
    return api


def _record():
    return PatientRecord.objects.get(person_id=PERSON_ID)


@pytest.mark.parametrize('field,inputs', [
    ('bmi', ['height', 'weight']),
    ('meets_crab', ['serum_calcium_mg_dl', 'creatinine_clearance_ml_min']),
    ('meets_slim', ['clonal_plasma_cells', 'kappa_flc']),
])
def test_a_direct_assignment_is_a_field_error(client, field, inputs):
    response = client.patch(NATIVE, {field: '1'}, format='json')

    assert response.status_code == 400, response.data
    assert field in response.data
    message = str(response.data[field])
    for name in inputs:
        assert name in message
    assert getattr(_record(), field) is None


def test_a_mixed_request_saves_nothing(client):
    record = _record()
    record.disease = 'CLL'
    record.save(update_fields=['disease'])

    response = client.patch(
        NATIVE, {'disease': 'multiple myeloma', 'bmi': 99}, format='json')

    assert response.status_code == 400, response.data
    record.refresh_from_db()
    assert record.disease == 'CLL'
    assert record.bmi is None


def test_an_unchanged_echo_is_not_an_assignment(client):
    record = _record()
    record.bmi = Decimal('20.76')
    record.save(update_fields=['bmi'])

    response = client.patch(NATIVE, {'bmi': '20.76'}, format='json')

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert float(record.bmi) == 20.76


def test_a_null_echo_of_an_empty_computed_field_is_not_an_assignment(client):
    response = client.patch(NATIVE, {'meets_crab': None}, format='json')

    assert response.status_code == 200, response.data


def test_the_source_inputs_still_edit_and_recompute(client):
    response = client.patch(
        NATIVE, {'height': 200, 'weight': 80}, format='json')

    assert response.status_code == 200, response.data
    record = _record()
    assert float(record.bmi) == 20.0


def test_hemoglobin_remains_a_clinical_input(client):
    response = client.patch(NATIVE, {'hemoglobin_g_dl': 8.5}, format='json')

    assert response.status_code == 200, response.data
    assert _record().hemoglobin_g_dl == Decimal('8.5')


def test_the_shared_form_refuses_the_same_assignment_atomically(client):
    record = _record()
    record.disease = 'CLL'
    record.save(update_fields=['disease'])

    response = client.patch(
        FORM, {'disease': 'multiple myeloma', 'meets_crab': True},
        format='json', HTTP_X_PATIENT_FORM='1')

    assert response.status_code == 400, response.data
    assert 'meets_crab' in response.data['rejected']
    record.refresh_from_db()
    assert record.disease == 'CLL'
    assert record.meets_crab is None


def test_the_shared_form_still_edits_the_source_inputs(client):
    response = client.patch(
        FORM, {'height': 200, 'weight': 80}, format='json',
        HTTP_X_PATIENT_FORM='1')

    assert response.status_code == 200, response.data
    assert float(_record().bmi) == 20.0


def test_the_classification_is_the_one_the_descriptor_and_serializer_use():
    descriptor = build_writable_field_descriptor()
    read_only = get_serializer_read_only_fields()

    for field in ('bmi', 'meets_crab', 'meets_slim'):
        assert field in computed_only_fields()
        assert field in read_only
        assert descriptor[field]['kind'] == 'computed'
        assert descriptor[field]['writable'] is False
    assert descriptor['meets_crab']['inputs'] == [
        'serum_calcium_mg_dl', 'creatinine_clearance_ml_min',
        'serum_creatinine_mg_dl', 'hemoglobin_g_dl', 'bone_lesions',
    ]


def test_the_shared_form_ignores_an_unchanged_echo_beside_a_real_edit(client):
    record = _record()
    record.meets_crab = True
    record.bmi = 20.0
    record.save(update_fields=['meets_crab', 'bmi'])

    response = client.patch(
        FORM, {'meets_crab': True, 'bmi': 20.0, 'weight': 75},
        format='json', HTTP_X_PATIENT_FORM='1')

    assert response.status_code == 200, response.data
    assert 'meets_crab' not in response.data['write']['rejected']
    assert 'bmi' not in response.data['write']['rejected']
    assert 'weight' in response.data['write']['applied']


@pytest.mark.parametrize('submitted', ['on', 't', 'y', '1', 'yes', 'TRUE'])
def test_boolean_spellings_of_a_changed_verdict_are_still_refused(client, submitted):
    record = _record()
    record.meets_crab = False
    record.save(update_fields=['meets_crab'])

    response = client.patch(
        NATIVE, {'meets_crab': submitted, 'disease': 'CLL'}, format='json')

    assert response.status_code == 400, (submitted, response.data)
    record.refresh_from_db()
    assert record.meets_crab is False
    assert record.disease != 'CLL'


@pytest.mark.parametrize('submitted', ['on', 't', 'y', 'bogus'])
def test_the_shared_form_refuses_boolean_spellings_too(client, submitted):
    record = _record()
    record.meets_crab = False
    record.save(update_fields=['meets_crab'])

    response = client.patch(
        FORM, {'meets_crab': submitted, 'disease': 'CLL'}, format='json',
        HTTP_X_PATIENT_FORM='1')

    assert response.status_code == 400, (submitted, response.data)
    record.refresh_from_db()
    assert record.meets_crab is False
    assert record.disease != 'CLL'


def test_an_equivalent_boolean_echo_is_still_allowed(client):
    record = _record()
    record.meets_crab = True
    record.save(update_fields=['meets_crab'])

    response = client.patch(NATIVE, {'meets_crab': 'on'}, format='json')

    assert response.status_code == 200, response.data
