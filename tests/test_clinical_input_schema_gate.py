"""Nullable source projections must not become projection-only API writes."""
import sys

import pytest
from rest_framework.exceptions import ValidationError

from omop_core.models import PatientRecord
from patient_portal.api.serializers import PatientRecordSerializer


@pytest.mark.parametrize('field,value', [
    ('prothrombin_time_uln', '1.25'),
    ('activated_partial_thromboplastin_time_uln', '1.75'),
    ('able_to_swallow_tablets_capsules', False),
])
def test_source_edit_rejected_without_sdk(monkeypatch, field, value):
    monkeypatch.setitem(sys.modules, 'omop_core.services.clinical_input_contract', None)
    serializer = PatientRecordSerializer(instance=PatientRecord(), partial=True)
    with pytest.raises(ValidationError) as exc:
        serializer.to_internal_value({field: value})
    assert field in exc.value.detail


@pytest.mark.django_db
def test_null_get_echo_is_not_an_authored_write():
    serializer = PatientRecordSerializer(instance=PatientRecord(), partial=True)
    assert serializer.to_internal_value({'prothrombin_time_uln': None}) == {}
