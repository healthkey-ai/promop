"""The standalone patient view edits canonical labs through the native PATCH (#2425).

This PR made the canonical lab columns normalised read-models: not `writable`,
edited through a value + unit source pair that only the federated form store can
render. The standalone /patient/:personId view has no store, so those labs
became read-only there. The server still honours a canonical write; these tests
pin that contract for every normalised read-model, and that a caller without
write access is told so.
"""
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from omop_core.models import PatientRecord
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.models import Identity
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db

CANONICAL = {
    'hemoglobin_g_dl': Decimal('9.5'),
    'serum_creatinine_mg_dl': Decimal('1.25'),
    'albumin_g_dl': Decimal('3.4'),
    'serum_calcium_mg_dl': Decimal('9.8'),
    'bilirubin_total_mg_dl': Decimal('1.2'),
    'anc_thousand_per_ul': Decimal('1.5'),
    'alc_thousand_per_ul': Decimal('0.9'),
    'platelet_count_thousand_per_ul': Decimal('150'),
}


@pytest.fixture
def staff(settings):
    settings.CELERY_BROKER_URL = ''
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='standalone-labs@example.test', is_staff=True))
    return client


def test_these_are_exactly_the_normalised_read_models():
    descriptor = build_writable_field_descriptor()
    marked = {name for name, entry in descriptor.items()
              if isinstance(entry, dict) and entry.get('normalised_read_model')}
    assert marked == set(CANONICAL)


@pytest.mark.parametrize('field, value', sorted(CANONICAL.items()))
def test_a_native_canonical_write_is_saved(staff, field, value):
    record = PatientRecordFactory()

    response = staff.patch(f'/api/patient-info/{record.person_id}/', {field: str(value)}, format='json')

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert Decimal(str(getattr(record, field))) == value


def test_a_native_canonical_write_retires_a_pending_hemoglobin_pair(staff):
    record = PatientRecordFactory()
    PatientRecord.objects.filter(pk=record.pk).update(
        hemoglobin_level=120, hemoglobin_level_units='G/L', hemoglobin_g_dl=12,
        user_edited_fields=['hemoglobin_level', 'hemoglobin_level_units'])

    response = staff.patch(f'/api/patient-info/{record.person_id}/', {'hemoglobin_g_dl': 9.5}, format='json')

    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert float(record.hemoglobin_g_dl) == 9.5
    assert float(record.hemoglobin_level) == 9.5
    assert record.hemoglobin_level_units == 'G/DL'
    assert not {'hemoglobin_level', 'hemoglobin_level_units'} & set(record.user_edited_fields or [])


def test_a_caller_without_write_access_sees_them_read_only():
    record = PatientRecordFactory()
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='no-write@example.test'))

    descriptor = client.get('/api/v1/patient-records/writable-fields/',
                            {'person_id': record.person_id}).data

    for field in CANONICAL:
        assert descriptor[field]['read_only_for_caller'] is True, field
        assert descriptor[field]['writable'] is False, field


def test_plain_descriptor_hosts_are_told_the_canonical_is_writable(staff):
    """EXACT's editabilityOf refuses `writable: false` and does not follow
    `inputs`; the native PATCH accepts the canonical. The shared form keeps the
    pair as its editor (#2418 regression review)."""
    record = PatientRecordFactory()
    url = '/api/v1/patient-records/writable-fields/'
    for params in ({}, {'person_id': record.person_id}):
        plain = staff.get(url, params).data
        shared = staff.get(url, params, HTTP_X_PATIENT_FORM='1').data
        for field in CANONICAL:
            assert plain[field]['writable'] is True, (field, params)
            assert shared[field]['writable'] is False, (field, params)
