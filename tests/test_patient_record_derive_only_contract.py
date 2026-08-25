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


def test_derivation_does_not_restore_legacy_user_edited_values():
    """A legacy metadata value must not override absent OMOP clinical facts."""
    derivation_service = (
        REPOSITORY_ROOT / "omop_core/services/patient_record_service.py"
    ).read_text()

    assert "user_edited_fields" not in derivation_service
    assert "_restore_user_edited" not in derivation_service


def test_public_contract_documents_read_only_mapped_fields_and_legacy_policy():
    """Documentation guards the distinction between source facts and projections."""
    api_surface = (REPOSITORY_ROOT / "API_SURFACE.md").read_text()
    architecture_brief = (
        REPOSITORY_ROOT / "docs/utah-rhtp-technical-architecture-brief.md"
    ).read_text()

    assert "Mapped PatientRecord fields are read-only." in api_surface
    assert "Profile/admin values that are\ndisplayed on PatientRecord" in api_surface
    assert "PatientRecord, such as email and validation metadata" in api_surface
    assert "PATCH /api/v1/persons/{person_id}/" in api_surface
    assert "Legacy SQL compatibility only:" in api_surface
    assert "New integrations must not query it" in api_surface
    assert "405 Method Not Allowed" in api_surface
    assert '"fields": ["hemoglobin_g_dl"]' in api_surface
    assert "docs/omop_to_patientrecord.md" in api_surface
    assert "The target state has no writable concrete PatientRecord clinical columns." in api_surface
    assert "`PATCH /api/v1/persons/{person_id}/`" in api_surface
    assert "PatientRecord fields are read-only at the PatientRecord API." in architecture_brief
    assert "Producers write" in architecture_brief
    assert "complete, provenance-bearing OMOP facts (or FHIR)" in architecture_brief


def test_partial_update_schema_description_explains_omop_write_migration():
    """The generated OpenAPI operation inherits this method documentation."""
    api_views = (REPOSITORY_ROOT / "patient_portal/api/views.py").read_text()

    assert "returns 405" in api_views
    assert "docs/omop_to_patientrecord.md" in api_views
    assert "Write the appropriate OMOP resource" in api_views


def test_every_concrete_patientrecord_data_column_is_api_read_only():
    """No pending derivation is a temporary PatientRecord write exception."""
    lifecycle_fields = {
        'id', 'person', 'organization', 'created_at', 'updated_at',
        'derived_at', 'derivation_version', 'user_edited_fields', 'custom_fields',
    }
    writable = {
        field.name for field in PatientRecord._meta.concrete_fields
        if field.name not in lifecycle_fields
        and field.name not in PATIENT_RECORD_OMOP_MAPPED_FIELDS
    }

    assert writable == set()
