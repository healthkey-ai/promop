"""The mapping editor must preserve table spelling and provenance on a notes edit."""
import pytest
from rest_framework.test import APIClient

from omop_core.models import FieldConceptMapping
from patient_portal.models import Identity


@pytest.mark.django_db
@pytest.mark.parametrize('field_name,table', [
    ('genetic_mutations.allelic_frequency', 'measurement'),
    ('hemoglobin_g_dl', 'Measurement'),
    ('smoking_status', 'observation'),
    ('smoking_status', 'condition_occurrence'),
])
def test_list_edit_and_reload_preserve_table_and_origin(field_name, table):
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(
        email='mapping-table@example.com', is_staff=True,
    ))
    mapping = FieldConceptMapping.objects.create(
        field_name=field_name, omop_table=table, concept_id=32817,
        vocabulary_id='Type Concept', concept_code='OMOP4976890',
        status='approved', provenance='system_generated',
    )

    response = client.get('/api/v1/field-mappings/')
    assert response.status_code == 200
    descriptor = next(item for item in response.data if item['field_name'] == field_name)
    initial = descriptor['mapping']
    assert initial['omop_table'] == table

    # Same payload as the dialog sends when the curator changes only Notes.
    response = client.patch(f'/api/v1/field-mappings/{mapping.pk}/', {
        'concept': initial['concept_id'],
        'vocabulary_id': initial['vocabulary_id'],
        'concept_code': initial['concept_code'],
        'unit': initial['unit'],
        'omop_table': initial['omop_table'],
        'status': initial['status'],
        'notes': 'Reviewed table',
    }, format='json')
    assert response.status_code == 200, response.data
    mapping.refresh_from_db()
    assert mapping.omop_table == table
    assert mapping.provenance == 'system_generated'
    assert mapping.status == 'approved'
    assert mapping.notes == 'Reviewed table'

    response = client.get('/api/v1/field-mappings/')
    assert response.status_code == 200
    reloaded = next(item for item in response.data if item['field_name'] == field_name)
    assert reloaded['mapping']['omop_table'] == table
