"""Mapping origin survives approval and transfer, but follows recipe corrections."""
import pytest

from omop_core.models import FieldConceptMapping
from omop_core.services.field_curation_transfer import read_payload, apply_payload
from omop_core.services.field_descriptor import get_all_field_descriptors
from patient_portal.api.serializers import FieldConceptMappingSerializer

pytestmark = pytest.mark.django_db


def test_api_cannot_spoof_system_origin():
    serializer = FieldConceptMappingSerializer(data={
        'field_name': 'hemoglobin_g_dl', 'provenance': 'system_generated',
    })
    assert serializer.is_valid(), serializer.errors
    row = serializer.save()
    assert row.provenance == 'curator'
    assert serializer.data['provenance'] == 'curator'


def test_approval_preserves_origin_but_recipe_edit_records_curator():
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance='system_generated',
    )
    serializer = FieldConceptMappingSerializer(row, data={'status': 'approved'}, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    row.refresh_from_db()
    assert row.provenance == 'system_generated'
    serializer = FieldConceptMappingSerializer(row, data={
        'unit': 'g/dL', 'provenance': 'system_generated',
    }, partial=True)
    assert serializer.is_valid(), serializer.errors
    serializer.save()
    row.refresh_from_db()
    assert row.provenance == 'curator'


def test_list_and_transfer_preserve_provenance_and_unknown_legacy():
    row = FieldConceptMapping.objects.create(
        field_name='hemoglobin_g_dl', provenance='system_generated',
    )
    descriptor = next(d for d in get_all_field_descriptors() if d['field_name'] == row.field_name)
    assert descriptor['mapping']['provenance'] == 'system_generated'
    payload = read_payload('default', tables={'mappings'})
    row.delete()
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').provenance == 'system_generated'
    payload['mappings'][0].pop('provenance')
    apply_payload(payload)
    assert FieldConceptMapping.objects.get(field_name='hemoglobin_g_dl').provenance == ''
