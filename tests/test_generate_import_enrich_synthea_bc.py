"""
Tests for generate_import_enrich_synthea_bc management command.
"""

import pytest
from django.core.management import call_command


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
