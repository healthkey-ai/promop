"""Validation errors must not expose exceptions from clinical parsing helpers."""
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from patient_portal.models import Identity
from tests.factories import PatientRecordFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def patient_api():
    record = PatientRecordFactory(
        tumor_grade='2', flipi_score_options='age', flipi_score=1,
    )
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='validation-review@example.test', is_staff=True,
    ))
    return client, record


CASES = [
    ('flipi_score_options', 'age', 'parse_factors', 'Select recognized FLIPI risk factors.'),
    ('tumor_grade', '3A', 'normalize_grade',
     'Use grade 1, 2, 3A, or 3B (3 for an unspecified historical grade).'),
]


@pytest.mark.parametrize('field,value,helper,message', CASES)
def test_parser_exception_details_are_not_returned(patient_api, field, value, helper, message):
    client, record = patient_api
    original = getattr(record, field)
    internal_detail = 'private-parser-detail /srv/internal/config.py SQL SELECT secret_column'
    with patch(f'omop_core.services.flipi.{helper}', side_effect=ValueError(internal_detail)):
        response = client.patch(f'/api/patient-info/{record.person_id}/',
                                {field: value}, format='json')
    assert response.status_code == 400
    assert response.json() == {field: [message]}
    assert internal_detail not in response.content.decode()
    record.refresh_from_db()
    assert getattr(record, field) == original


@pytest.mark.parametrize('field,value,helper,message', CASES)
def test_invalid_assessment_returns_actionable_static_error(patient_api, field, value, helper, message):
    client, record = patient_api
    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {field: 'invalid'}, format='json')
    assert response.status_code == 400
    assert response.json() == {field: [message]}


@pytest.mark.parametrize('field,value,expected', [
    ('flipi_score_options', 'age,age,Elevated LDH', 'age,ldh'),
    ('flipi_score_options', '', ''),
    ('flipi_score_options', None, None),
    ('tumor_grade', 'grade 3a', '3A'),
    ('tumor_grade', '3B', '3B'),
    ('tumor_grade', None, None),
])
def test_valid_assessments_still_normalize_and_save(patient_api, field, value, expected):
    client, record = patient_api
    response = client.patch(f'/api/patient-info/{record.person_id}/',
                            {field: value}, format='json')
    assert response.status_code == 200, response.data
    record.refresh_from_db()
    assert getattr(record, field) == expected
