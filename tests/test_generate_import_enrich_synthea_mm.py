"""
Tests for generate_import_enrich_synthea_mm management command.
"""

import pytest
from django.core.management import call_command


def test_wrapper_runs_generation_import_and_enrichment_in_order(monkeypatch):
    calls = []

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.call_command',
        fake_call_command,
    )

    call_command(
        'generate_import_enrich_synthea_mm',
        count=100,
        output='/tmp/synthea_mm_100.json',
        org_slug='synthea-mm',
        seed=42,
        rrmm_ratio=0.85,
        import_batch_size=5,
        enrich_limit=10,
        min_procedures=2,
    )

    assert len(calls) == 3, f'Expected 3 call_command calls, got {len(calls)}'

    # Step 1: generate_fhir_bundle with disease=mm
    assert calls[0][0] == 'generate_fhir_bundle'
    assert calls[0][1]['disease'] == 'mm'
    assert calls[0][1]['count'] == 100
    assert calls[0][1]['output'] == '/tmp/synthea_mm_100.json'
    assert calls[0][1]['seed'] == 42
    assert calls[0][1]['rrmm_ratio'] == 0.85

    # Step 2: import_fhir_bundle
    assert calls[1][0] == 'import_fhir_bundle'
    assert calls[1][1]['file'] == '/tmp/synthea_mm_100.json'
    assert calls[1][1]['org_slug'] == 'synthea-mm'
    assert calls[1][1]['batch_size'] == 5

    # Step 3: enrich_synthea_mm_omop_data
    assert calls[2][0] == 'enrich_synthea_mm_omop_data'
    assert calls[2][1]['org_slugs'] == 'synthea-mm'
    assert calls[2][1]['confirm'] is True
    assert calls[2][1]['limit'] == 10
    assert calls[2][1]['min_procedures'] == 2


def test_wrapper_forwards_rrmm_ratio_to_generator(monkeypatch):
    calls = []

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.call_command',
        fake_call_command,
    )

    call_command('generate_import_enrich_synthea_mm', rrmm_ratio=0.95)

    generate_call = calls[0]
    assert generate_call[0] == 'generate_fhir_bundle'
    assert generate_call[1]['rrmm_ratio'] == 0.95
    assert generate_call[1]['disease'] == 'mm'


def test_wrapper_omits_seed_when_not_provided(monkeypatch):
    calls = []

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.call_command',
        fake_call_command,
    )

    call_command('generate_import_enrich_synthea_mm')

    generate_kwargs = calls[0][1]
    assert 'seed' not in generate_kwargs


def test_wrapper_can_wipe_existing_org_before_regenerating(monkeypatch):
    calls = []

    class _ExistingOrg:
        slug = 'synthea-mm'

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.call_command',
        fake_call_command,
    )
    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.Organization.objects.filter',
        lambda **kwargs: type('Q', (), {'first': lambda self=None: _ExistingOrg()})(),
    )
    wiped = []
    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.delete_organization_with_patient_cascade',
        lambda org: wiped.append(org.slug),
    )

    call_command(
        'generate_import_enrich_synthea_mm',
        org_slug='synthea-mm',
        wipe_existing=True,
    )

    assert wiped == ['synthea-mm']
    assert calls[0][0] == 'generate_fhir_bundle'
    assert calls[1][0] == 'import_fhir_bundle'
    assert calls[2][0] == 'enrich_synthea_mm_omop_data'


def test_wrapper_default_org_slug_is_synthea_mm(monkeypatch):
    calls = []

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.call_command',
        fake_call_command,
    )

    call_command('generate_import_enrich_synthea_mm')

    assert calls[1][1]['org_slug'] == 'synthea-mm'
    assert calls[2][1]['org_slugs'] == 'synthea-mm'


def test_wrapper_enrich_limit_omitted_by_default(monkeypatch):
    calls = []

    def fake_call_command(name, *args, **kwargs):
        calls.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_mm.call_command',
        fake_call_command,
    )

    call_command('generate_import_enrich_synthea_mm')

    enrich_kwargs = calls[2][1]
    assert 'limit' not in enrich_kwargs
