"""Regression checks for the PatientRecord mapped-field ownership contract."""

from pathlib import Path


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


def test_public_contract_documents_read_only_mapped_fields_and_legacy_policy():
    """Documentation guards the distinction between source facts and projections."""
    api_surface = (REPOSITORY_ROOT / "API_SURFACE.md").read_text()
    architecture_brief = (
        REPOSITORY_ROOT / "docs/utah-rhtp-technical-architecture-brief.md"
    ).read_text()

    assert "Mapped clinical fields on PatientRecord are read-only." in api_surface
    assert "It rejects mapped clinical fields." in api_surface
    assert "projection-owned field with no OMOP representation" in api_surface
    assert "Legacy SQL compatibility only:" in api_surface
    assert "New integrations must not query it" in api_surface
    assert "Mapped clinical PatientRecord fields are read-only at the PatientRecord API." in architecture_brief
    assert "Producers write complete, provenance-bearing OMOP facts (or FHIR)" in architecture_brief
