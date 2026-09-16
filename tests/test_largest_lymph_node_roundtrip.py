"""The Largest Lymph Node editor writes the standard Cancer Modifier fact."""

import pytest
from rest_framework.test import APIClient

from omop_core.models import Measurement
from omop_core.services.patient_record_service import refresh_patient_record
from omop_core.services.write_descriptor import build_writable_field_descriptor
from patient_portal.models import Identity
from tests.factories import ConceptFactory, PatientRecordFactory, VocabularyFactory


pytestmark = pytest.mark.django_db


def test_largest_lymph_node_descriptor_to_measurement_to_patient_record():
    """Exercise the same descriptor and POST shape used by the clinical editor."""
    record = PatientRecordFactory()
    concept = ConceptFactory(
        concept_id=36769292,
        concept_code='largest-lymph-node-dimension',
        concept_name='Dimension of Largest Lymph Node',
        vocabulary=VocabularyFactory(vocabulary_id='Cancer Modifier'),
    )
    type_concept = ConceptFactory(
        concept_id=32865,
        concept_code='Patient reported',
        concept_name='Patient reported',
    )
    descriptor = build_writable_field_descriptor()['largest_lymph_node_size']
    projection = descriptor['projection']

    assert projection['concept_id'] == concept.concept_id

    client = APIClient()
    user = Identity.objects.create_user(
        email='lymph-node-editor@example.test', is_staff=True,
    )
    client.force_authenticate(user=user)
    response = client.post('/api/v1/measurements/', {
        'person': record.person_id,
        'measurement_concept': projection['concept_id'],
        'measurement_date': '2026-09-07',
        'measurement_type_concept': type_concept.concept_id,
        'measurement_source_value': projection['source_value'],
        'value_as_number': 2.7,
        'unit_source_value': projection.get('unit', 'cm'),
    }, format='json')

    assert response.status_code == 201, response.data
    measurement = Measurement.objects.get(measurement_id=response.data['measurement_id'])
    assert measurement.measurement_concept_id == 36769292
    assert measurement.measurement_source_value == concept.concept_code
    assert measurement.qualifier_source_value is None

    refreshed = refresh_patient_record(record.person)
    assert refreshed.largest_lymph_node_size == pytest.approx(2.7)
