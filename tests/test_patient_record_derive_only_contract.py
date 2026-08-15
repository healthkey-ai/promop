"""Regression checks for the PatientRecord compatibility-write contract."""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_public_contract_documents_compatibility_mapping_and_legacy_policy():
    """Documentation guards the distinction between source facts and projections."""
    api_surface = (REPOSITORY_ROOT / "API_SURFACE.md").read_text()
    architecture_brief = (
        REPOSITORY_ROOT / "docs/utah-rhtp-technical-architecture-brief.md"
    ).read_text()

    assert "PatientRecord compatibility PATCH accepts supported mapped clinical tuples" in api_surface
    assert "writes the corresponding OMOP fact, and refreshes `PatientRecord` from OMOP" in api_surface
    assert "projection-owned field with no OMOP representation" in api_surface
    assert "Legacy SQL compatibility only:" in api_surface
    assert "New integrations must not query it" in api_surface
    assert "compatibility API deliberately maps supported mapped-clinical tuples to" in architecture_brief
    assert "complete, provenance-bearing OMOP facts before refreshing `PatientRecord`" in architecture_brief
