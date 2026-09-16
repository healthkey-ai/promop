"""Contract tests for the Mapping bounded-context import boundary."""

from omop_core.mapping import code_resolution, field, suggestions, therapy
from omop_core.services import code_mapping, mapping_suggestions, regimen_resolution


def test_legacy_code_resolution_imports_remain_compatible():
    assert code_mapping.resolve_source_code is code_resolution.resolve_source_code
    assert code_mapping.repoint_clinical_rows is code_resolution.repoint_clinical_rows


def test_legacy_suggestion_imports_remain_compatible():
    assert mapping_suggestions.suggest_mappings is suggestions.suggest_mappings
    assert mapping_suggestions.suggest_source_code is suggestions.suggest_source_code


def test_legacy_therapy_imports_remain_compatible():
    assert regimen_resolution.validate_hemonc_regimen is therapy.validate_hemonc_regimen
    assert regimen_resolution.get_or_create_quarantine_regimen is therapy.get_or_create_quarantine_regimen


def test_field_capabilities_are_available_from_mapping_component():
    assert callable(field.get_all_field_descriptors)
    assert callable(field.coerce_assertion_value)
    assert callable(field.read_payload)
    assert callable(field.apply_payload)
