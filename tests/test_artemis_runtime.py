"""Guardrails for the checked-in, non-production ARTEMIS runner."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "ops" / "artemis"


def test_artemis_runtime_is_pinned_and_not_an_rstudio_service():
    dockerfile = (RUNTIME / "Dockerfile").read_text()

    assert "ARTEMIS_REF=242b5a24864b85a44c62d95a98cbaa2d16c55539" in dockerfile
    assert "rocker/r-ver:4.4.3" in dockerfile
    assert "EXPOSE" not in dockerfile
    assert "USER artemis" in dockerfile


def test_artemis_runner_defaults_to_no_database_execution():
    runner = (RUNTIME / "run_artemis.R").read_text()

    assert 'ARTEMIS_MODE", unset = "dry-run"' in runner
    assert 'ARTEMIS_NONPROD_APPROVED", unset = "") != "yes"' in runner
    assert 'ARTEMIS_ALLOW_WRITE", unset = "") != "yes"' in runner
    assert "dry-run-validated-no-database-connection" in runner


def test_artemis_runner_emits_the_materializer_json_contract_fixture():
    runner = (RUNTIME / "run_artemis.R").read_text()
    fixture = json.loads((RUNTIME / "fixtures" / "artemis-episodes.json").read_text())

    assert '"/work/output/artemis-episodes.json"' in runner
    assert 'schema_version = "1"' in runner
    assert "drug_exposure_ids" in runner
    assert "Cannot prove a unique local drug_exposure_id" in runner
    assert "CompleteDrugRecord" in runner
    assert "Refusing to overwrite artemis-episodes.json" in runner
    assert fixture["schema_version"] == "1"
    assert set(fixture["episodes"][0]) == {
        "person_id", "line_number", "start_date", "end_date", "drug_exposure_ids",
    }


def test_artemis_runbook_covers_the_required_operational_controls():
    runbook = (ROOT / "docs" / "runbooks" / "artemis.md").read_text()

    for heading in (
        "## Inputs",
        "## Build and dry run",
        "## Non-production acceptance execution",
        "## Validation and audit",
        "## Rollback and incident response",
    ):
        assert heading in runbook
