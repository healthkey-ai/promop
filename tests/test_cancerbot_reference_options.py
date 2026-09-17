import copy
import json
from pathlib import Path

import pytest

from omop_core.services.cancerbot_reference_options import ReferenceOptions, validate_reference_tables
from omop_core.services.field_inventory import validate_manifest, coverage
from omop_core.management.commands.export_cancerbot_reference_options import build_reference_export
from omop_core.management.commands.import_field_inventory_reference_options import merge_reference_options

ROOT = Path(__file__).resolve().parents[1] / 'docs/field-mapping-inventory'


@pytest.fixture
def snapshot():
    return json.loads((ROOT / 'cancerbot-reference.json').read_text())


@pytest.fixture
def manifest():
    return json.loads((ROOT / 'manifest.json').read_text())


def provider(snapshot):
    return ReferenceOptions(snapshot['provider_source'], snapshot['tables'])


def test_reviewed_source_and_reference_rows_reproduce_all_live_lists(snapshot, manifest):
    result = build_reference_export(manifest, snapshot['provider_source'], snapshot)
    assert result == json.loads((ROOT / 'cancerbot-options.json').read_text())
    assert len(result['options']) == 118


def test_provider_source_drift_requires_new_review(snapshot):
    with pytest.raises(ValueError, match='source changed'):
        ReferenceOptions(snapshot['provider_source'] + '\n', snapshot['tables'])


@pytest.mark.parametrize('fault', ['patient_table', 'patient_column', 'missing_column', 'duplicate_id', 'orphan'])
def test_reference_snapshot_rejects_non_reference_and_corrupt_data(snapshot, fault):
    tables = snapshot['tables']
    if fault == 'patient_table': tables['trials_patientinfo'] = []
    elif fault == 'patient_column': tables['trials_disease'][0]['patient_id'] = 1
    elif fault == 'missing_column': del tables['trials_disease'][0]['code']
    elif fault == 'duplicate_id': tables['trials_disease'].append(tables['trials_disease'][0])
    else: tables['trials_mutationcode'][0]['gene_id'] = -999
    with pytest.raises(ValueError): validate_reference_tables(tables)


def test_snapshot_requires_recorded_read_only_transaction(snapshot, manifest):
    snapshot['transaction_read_only'] = 'off'
    with pytest.raises(ValueError, match='read-only'):
        build_reference_export(manifest, snapshot['provider_source'], snapshot)


def test_planned_eligibility_uses_its_own_catalog_and_nullable_round(snapshot):
    tables = snapshot['tables']
    tables['trials_disease'] = [{'id':1,'code':'MM','title':'MM'}, {'id':2,'code':'BC','title':'BC'}]
    tables['trials_therapyround'] = [{'id':1,'code':'later_therapy','title':'Later'}, {'id':2,'code':'first_line_therapy','title':'First'}]
    tables['trials_plannedtherapy'] = [{'id':i,'code':f'p{i}','title':f'Plan {i}'} for i in range(1,5)]
    tables['trials_plannedtherapydiseaseconnection'] = [
        {'id':1,'planned_therapy_id':1,'disease_id':1,'round_id':None},
        {'id':2,'planned_therapy_id':2,'disease_id':1,'round_id':1},
        {'id':3,'planned_therapy_id':3,'disease_id':1,'round_id':2},
        {'id':4,'planned_therapy_id':4,'disease_id':2,'round_id':1},
    ]
    p = provider(snapshot)
    assert p.method('planned_therapies', ['mm','later_therapy']) == {'none':'No planned therapy','p1':'Plan 1','p2':'Plan 2'}
    assert set(p.method('planned_therapies', ['MM','later_line_therapy'])) == {'none','p1'}
    assert set(p.method('planned_therapies', ['MM'])) == {'none','p1','p2','p3'}


def test_genetic_membership_keeps_independent_gene_origins_and_unknown(snapshot):
    t = snapshot['tables']
    t['trials_mutationgene'] = [{'id':1,'code':'g1','title':'Gene 1'}, {'id':2,'code':'g2','title':'Gene 2'}]
    t['trials_mutationcode'] = [{'id':1,'code':'v1','title':'Variant 1','gene_id':1}]
    t['trials_mutationorigin'] = [{'id':1,'code':'somatic','title':'Somatic'}]
    t['trials_mutationgeneoriginconnection'] = [{'id':1,'gene_id':1,'origin_id':1}]
    p = provider(snapshot)
    origins = p.method('mutation_origins_per_gene', [])
    assert origins['g2'] == []
    assert origins['g1'] == [{'value':'','label':'Unknown'}, {'value':'somatic','label':'Somatic'}]
    assert p.method('all_mutation_variants', [])['g2'] == [{'value':'','label':'Unknown'}]
    assert p.method('mutation_all_origins', []) == {'':'Unknown','somatic__g1':'Somatic Gene 1','somatic__v1':'Somatic Variant 1'}


def test_marker_none_and_numeric_toxicity_are_not_unknown_strings(snapshot):
    p=provider(snapshot)
    assert p.method('cytogenic_markers', [])['none'] == 'None'
    assert '' not in p.method('cytogenic_markers', [])
    values=p.method('toxicity_grade', [])
    assert values[''] == 'Unknown'
    assert 0 in values and '0' not in values


def test_therapy_graph_ignores_null_component_links_without_inventing_rows(snapshot):
    p=provider(snapshot)
    all_components=p.method('therapy_components_all', [])
    assert '' not in all_components
    therapies=p.method('therapies_by_disease_code_and_line_code',['MM','first_line_therapy'])
    assert therapies[''] == 'Unknown/Other'
    assert therapies['vrd'].startswith('VRd (')
    assert 'Bortezomib' in therapies['vrd']
    assert 'growth_factors' in p.method('therapy_types_all', [])


def test_import_is_idempotent_preserves_evidence_and_does_not_mutate_input(snapshot, manifest):
    payload=build_reference_export(manifest,snapshot['provider_source'],snapshot)
    original=copy.deepcopy(manifest)
    first=merge_reference_options(manifest,payload)
    assert manifest == original
    assert merge_reference_options(first,payload) == first
    for key in ['reference_tables','candidates','maps_to','vocabulary_metadata','vocabulary_releases']:
        assert first[key] == original[key]
    coverage_state=first['totals']['source_coverage']['cancerbot_public_lists']
    assert coverage_state['missing_live_lists'] == []
    assert coverage_state['by_provider']['covered_by_live_export'] == 118
    assert not first['complete']
    validate_manifest(first)


def test_partial_refresh_preserves_other_lists_and_removed_membership_history(snapshot, manifest):
    payload=build_reference_export(manifest,snapshot['provider_source'],snapshot)
    initial=merge_reference_options(manifest,payload)
    row=next(r for r in initial['rows'] if r['source']=='cancerbot_live' and r['option_list']=='ethnicity')
    row['reason']='Curator evidence retained'
    other=next(b for b in initial['cancerbot_bindings'] if b['option_list']=='geneticMutationAllVariants')
    partial={**payload,'options':{'ethnicity':{'options':[]}}}
    refreshed=merge_reference_options(initial,partial)
    retained=next(r for r in refreshed['rows'] if r['id']==row['id'])
    assert retained['reason']=='Curator evidence retained'
    assert retained['source_presence']=='absent_from_latest_export'
    assert retained['retired'] is None
    assert next(b for b in refreshed['cancerbot_bindings'] if b['option_list']==other['option_list']) == other
    assert next(b for b in refreshed['cancerbot_bindings'] if b['option_list']=='ethnicity')['live_source_row_ids']==[]


@pytest.mark.parametrize('source_changed', [False, True])
def test_new_reference_rows_get_routes_only_for_reviewed_source_without_rewriting_decisions(manifest, source_changed):
    from omop_core.services.field_inventory_contracts import implementation_contracts
    from omop_core.services.field_inventory_crosswalk import reviewed_routes
    for route in manifest['destination_crosswalk']['bindings']:
        route.update(reviewed_routes()[route['option_list']])
    manifest['implementation_contracts'] = implementation_contracts(
        manifest['rows'], manifest['destination_crosswalk'], manifest['cancerbot_bindings'],
        manifest['frontend'], manifest['representation_decisions'])
    manifest['totals'] = coverage(manifest['rows'], manifest['totals']['source_coverage'])
    reviewed = next(r for r in manifest['rows'] if r['source'] == 'cancerbot_live' and r['option_list'] == 'ethnicity')
    reviewed['reason'] = 'Existing curator rationale'
    original = copy.deepcopy(reviewed)
    payload = {'schema_version': 1, 'exported_at': '2026-09-14T00:00:00Z',
               'source_revision': 'changed-source' if source_changed else manifest['source_revisions']['cancerbot']['revision'],
               'options': {'ethnicity': {'options': [{'value': 'new-local-value', 'label': 'New local value'}]}}}
    result = merge_reference_options(manifest, payload)
    retained = next(r for r in result['rows'] if r['id'] == reviewed['id'])
    for key in ('reason', 'disposition', 'owning_issue', 'existing_mappings', 'canonical_value'):
        assert retained[key] == original[key]
    new = next(r for r in result['rows'] if r['source_key']['value'] == 'new-local-value')
    contract = new['implementation_contract']
    assert new['candidate_ids'] == []
    assert contract['semantic_approval'] is False
    if source_changed:
        assert contract['status'] == 'destination_unresolved'
        assert contract['destinations'] == []
        assert result['implementation_contracts']['source_review_required']
        assert result['destination_crosswalk']['status'] == 'source_revision_requires_review'
        assert 'destination_route_keys' not in retained
    else:
        assert contract['destinations'] == ['ethnicity', 'race']
        assert contract['implementation_owners'] == ['#1228']
        assert new['disposition'] == 'ambiguous'
    assert merge_reference_options(result, payload) == result
