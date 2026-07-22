"""
Tests for generate_import_enrich_synthea_fl management command.
"""

import pytest
from django.core.management import call_command


@pytest.fixture
def calls(monkeypatch):
    log = []

    def fake_call_command(name, *args, **kwargs):
        log.append((name, kwargs))

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_fl.call_command',
        fake_call_command,
    )
    return log


def test_wrapper_runs_generation_import_and_enrichment_in_order(calls):
    call_command(
        'generate_import_enrich_synthea_fl',
        count=100,
        output='/tmp/synthea_fl_100.json',
        org_slug='synthea-fl',
        seed=42,
        import_batch_size=5,
        enrich_limit=10,
    )

    assert len(calls) == 3, f'Expected 3 call_command calls, got {len(calls)}'

    # Step 1: generate_fhir_bundle with disease=fl
    assert calls[0][0] == 'generate_fhir_bundle'
    assert calls[0][1]['disease'] == 'fl'
    assert calls[0][1]['count'] == 100
    assert calls[0][1]['output'] == '/tmp/synthea_fl_100.json'
    assert calls[0][1]['seed'] == 42

    # Step 2: import_fhir_bundle
    assert calls[1][0] == 'import_fhir_bundle'
    assert calls[1][1]['file'] == '/tmp/synthea_fl_100.json'
    assert calls[1][1]['org_slug'] == 'synthea-fl'
    assert calls[1][1]['batch_size'] == 5

    # Step 3: enrich_synthea_fl_omop_data
    assert calls[2][0] == 'enrich_synthea_fl_omop_data'
    assert calls[2][1]['org_slugs'] == 'synthea-fl'
    assert calls[2][1]['confirm'] is True
    assert calls[2][1]['limit'] == 10


def test_wrapper_forwards_watch_wait_ratio(calls):
    call_command('generate_import_enrich_synthea_fl', watch_wait_ratio=0.35)
    assert calls[0][0] == 'generate_fhir_bundle'
    assert calls[0][1]['watch_wait_ratio'] == 0.35


def test_wrapper_omits_optional_generator_kwargs_by_default(calls):
    call_command('generate_import_enrich_synthea_fl')
    generate_kwargs = calls[0][1]
    assert 'seed' not in generate_kwargs
    assert 'watch_wait_ratio' not in generate_kwargs
    enrich_kwargs = calls[2][1]
    assert 'limit' not in enrich_kwargs


def test_wrapper_default_org_slug_is_synthea_fl(calls):
    call_command('generate_import_enrich_synthea_fl')
    assert calls[1][1]['org_slug'] == 'synthea-fl'
    assert calls[2][1]['org_slugs'] == 'synthea-fl'


def test_wrapper_can_wipe_existing_org_before_regenerating(calls, monkeypatch):
    class _ExistingOrg:
        slug = 'synthea-fl'

    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_fl.Organization.objects.filter',
        lambda **kwargs: type('Q', (), {'first': lambda self=None: _ExistingOrg()})(),
    )
    wiped = []
    monkeypatch.setattr(
        'omop_core.management.commands.generate_import_enrich_synthea_fl.delete_organization_with_patient_cascade',
        lambda org: wiped.append(org.slug),
    )

    call_command(
        'generate_import_enrich_synthea_fl',
        org_slug='synthea-fl',
        wipe_existing=True,
    )

    assert wiped == ['synthea-fl']
    assert calls[0][0] == 'generate_fhir_bundle'
    assert calls[1][0] == 'import_fhir_bundle'
    assert calls[2][0] == 'enrich_synthea_fl_omop_data'
