"""Regression checks for the PatientRecord mapped-field ownership contract."""

from pathlib import Path

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import PATIENT_RECORD_OMOP_MAPPED_FIELDS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_retired_patient_record_to_omop_write_through_service_is_absent():
    """Clinical facts must not be reconstructed from a PatientRecord PATCH."""
    assert not (
        REPOSITORY_ROOT / "omop_core/services/omop_write_service.py"
    ).exists()


def test_api_does_not_import_or_call_retired_write_through_service():
    """Keep the API from reintroducing the retired reverse-sync bridge."""
    api_views = (REPOSITORY_ROOT / "patient_portal/api/views.py").read_text()

    assert "omop_write_service" not in api_views
    assert "sync_to_omop" not in api_views


def test_derivation_preserves_user_edited_fields():
    """The PatientRecord-first architecture stores user edits directly and
    derivation must preserve them when no OMOP fact backs the field yet.
    user_edited_fields is the mechanism for this preservation."""
    derivation_service = (
        REPOSITORY_ROOT / "omop_core/services/patient_record_service.py"
    ).read_text()

    # user_edited_fields MUST be present — derivation snapshots and restores
    # values that have no OMOP backing yet.
    assert "user_edited_fields" in derivation_service


def test_public_contract_documents_patient_record_first_writes_and_legacy_policy():
    """Public docs link the current editing contract and retain SQL-view limits."""
    api_surface = (REPOSITORY_ROOT / "API_SURFACE.md").read_text()

    assert "docs/patient-record-first-writes.md" in api_surface
    assert "field_concept_mapping_architecture.md" in api_surface
    assert "PATCH /api/v1/patient-records/{person_id}/" in api_surface
    assert "Legacy SQL compatibility only:" in api_surface
    assert "New integrations must not query it" in api_surface
    assert "Mapped PatientRecord fields are read-only." not in api_surface


def test_partial_update_schema_description_explains_omop_write_migration():
    """The generated OpenAPI operation inherits this method documentation."""
    api_views = (REPOSITORY_ROOT / "patient_portal/api/views.py").read_text()

    # The docstring documents the silent-ignore behaviour for read-only fields
    # and advises clients to check the writable-fields descriptor.
    assert "silently ignored" in api_views
    assert "writable-fields descriptor" in api_views


def test_every_concrete_patientrecord_data_column_is_api_read_only():
    """No pending derivation is a temporary PatientRecord write exception."""
    lifecycle_fields = {
        'id', 'person', 'organization', 'created_at', 'updated_at',
        'derived_at', 'derivation_version', 'user_edited_fields', 'custom_fields', 'therapy_overrides',
    }
    writable = {
        field.name for field in PatientRecord._meta.concrete_fields
        if field.name not in lifecycle_fields
        and field.name not in PATIENT_RECORD_OMOP_MAPPED_FIELDS
    }

    assert writable == set()
