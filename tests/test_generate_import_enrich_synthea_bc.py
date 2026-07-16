"""
Tests for generate_import_enrich_synthea_bc management command.
"""

from datetime import datetime

import pytest
from django.core.management import call_command


def test_therapy_medication_statement_emits_line_and_outcome_extensions():
    from omop_core.management.commands.generate_synthea_bc import (
        _make_therapy_medication_statement,
    )

    res = _make_therapy_medication_statement(
        'Patient/1', 2, 'AC-T', 35101507,
        ['doxorubicin', 'cyclophosphamide', 'paclitaxel'],
        datetime(2024, 1, 1), datetime(2024, 6, 1), 'Complete Response',
    )

    # One regimen-level statement + one per drug.
    assert len(res) == 4
    regimen = res[0]
    assert regimen['resourceType'] == 'MedicationStatement'
    assert 'partOf' not in regimen
    exts = {e['url'].rsplit('/', 1)[-1]: e for e in regimen['extension']}
    assert exts['therapy-line']['valueInteger'] == 2
    # Outcome is the human-readable name, not a SNOMED code.
    assert exts['therapy-outcome']['valueString'] == 'Complete Response'
    # HemOnc coding is present so import resolves the regimen concept.
    assert any(
        c['system'].endswith('HemOnc') and c['code'] == '35101507'
        for c in regimen['medicationCodeableConcept']['coding']
    )
    # Drug sub-statements reference the regimen via partOf and carry the line.
    for drug_stmt in res[1:]:
        assert drug_stmt['partOf'] == [{'reference': f"urn:uuid:{regimen['id']}"}]
        assert drug_stmt['extension'][0]['valueInteger'] == 2


def test_therapy_medication_statement_omits_hemonc_when_id_absent():
    from omop_core.management.commands.generate_synthea_bc import (
        _make_therapy_medication_statement,
    )

    res = _make_therapy_medication_statement(
        'Patient/1', 1, 'Palbociclib+AI', None,
        ['palbociclib', 'letrozole'],
        datetime(2024, 1, 1), datetime(2025, 1, 1), 'Stable Disease',
    )
    codings = res[0]['medicationCodeableConcept']['coding']
    assert not any(c['system'].endswith('HemOnc') for c in codings)


def test_wrapper_runs_generation_import_and_enrichment_in_order(monkeypatch):
    calls = []

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_bc.call_command',
        fake_call_command,
    )

    call_command(
        'generate_import_enrich_synthea_bc',
        count=100,
        output='/tmp/synthea_bc_100_codex.json',
        org_slug='synthea-bc',
        seed=42,
        deceased_fraction=0.2,
        import_batch_size=5,
        enrich_limit=10,
    )

    assert calls[0][0] == 'generate_synthea_bc'
    assert calls[0][1]['count'] == 100
    assert calls[0][1]['output'] == '/tmp/synthea_bc_100_codex.json'
    assert calls[0][1]['seed'] == 42
    assert calls[0][1]['deceased_fraction'] == 0.2

    assert calls[1][0] == 'import_fhir_bundle'
    assert calls[1][1]['file'] == '/tmp/synthea_bc_100_codex.json'
    assert calls[1][1]['org_slug'] == 'synthea-bc'
    assert calls[1][1]['batch_size'] == 5

    assert calls[2][0] == 'enrich_breast_cancer_omop_data'
    assert calls[2][1]['org_slugs'] == 'synthea-bc'
    assert calls[2][1]['confirm'] is True
    assert calls[2][1]['limit'] == 10


def test_wrapper_can_wipe_existing_org_before_regenerating(monkeypatch):
    calls = []

    class _ExistingOrg:
        slug = 'synthea-bc'

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_bc.call_command',
        fake_call_command,
    )
    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_bc.Organization.objects.filter',
        lambda **kwargs: type('Q', (), {'first': lambda self=None: _ExistingOrg()})(),
    )
    wiped = []
    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_bc.delete_organization_with_patient_cascade',
        lambda org: wiped.append(org.slug),
    )

    call_command(
        'generate_import_enrich_synthea_bc',
        org_slug='synthea-bc',
        wipe_existing=True,
    )

    assert wiped == ['synthea-bc']
    assert calls[0][0] == 'generate_synthea_bc'
    assert calls[1][0] == 'import_fhir_bundle'
    assert calls[2][0] == 'enrich_breast_cancer_omop_data'
