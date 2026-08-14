"""Regression checks for the PatientRecord derived-read-model boundary."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_patient_record_to_omop_write_through_service_is_absent():
    """Clinical facts must not be reconstructed from a projection field PATCH."""
    assert not (
        REPOSITORY_ROOT / "omop_core/services/omop_write_service.py"
    ).exists()


def test_api_does_not_import_or_call_patient_record_write_through():
    """Keep the API from reinstating the removed projection-to-OMOP bridge."""
    api_views = (REPOSITORY_ROOT / "patient_portal/api/views.py").read_text()

    assert "omop_write_service" not in api_views
    assert "sync_to_omop" not in api_views


def test_public_contract_documents_mapped_field_policy_and_legacy_patient_info_policy():
    """Documentation guards against new clinical projection writers/consumers."""
    api_surface = (REPOSITORY_ROOT / "API_SURFACE.md").read_text()
    architecture_brief = (
        REPOSITORY_ROOT / "docs/utah-rhtp-technical-architecture-brief.md"
    ).read_text()

    assert "Mapped clinical fields on `PatientRecord` are read-only." in api_surface
    assert "projection-owned field with no OMOP representation" in api_surface
    assert "Legacy SQL compatibility only:" in api_surface
    assert "New integrations must not query it" in api_surface
    assert "translates each field into the" not in api_surface
    assert "There is deliberately no mapped-clinical-`PatientRecord`-to-OMOP write-through" in architecture_brief
