from copy import deepcopy

from omop_core.services.field_inventory import inventory_row
from omop_core.services.field_inventory_contracts import implementation_contracts


def field(name, category='needs-concept-set'):
    row = inventory_row('promop_field', 'PatientRecord', name, name, field=name, kind='field')
    row['descriptor_category'] = category
    return row


def test_shared_fragment_keeps_typed_identity_and_all_source_context_routes():
    row = inventory_row('cancerbot_source', 'stages:literal:0', 0, 'Unknown', value=0)
    original = deepcopy(row)
    routes = [{'option_list': name, 'status': 'source_route_recorded', 'destination_candidates': ['stage'],
               'representation': 'context_required', 'owning_issue': '#1228', 'context': {'disease': disease}}
              for name, disease in [('stagesMm', 'MM'), ('stagesCll', 'CLL')]]
    bindings = [{'option_list': r['option_list'], 'static_resolution': {'dependencies': [{'method': 'stages'}]}}
                for r in routes]
    implementation_contracts([row, field('stage')], {'bindings': routes}, bindings, {})
    assert row['implementation_contract']['route_keys'] == ['stagesCll', 'stagesMm']
    assert row['disposition'] == 'ambiguous'
    assert row['owning_issue'] == '#1228'
    for key in ('id', 'canonical_value', 'scope', 'aliases', 'candidate_ids', 'existing_mappings'):
        assert row[key] == original[key]
    assert row['destination_path'] is None


def test_known_destinations_do_not_erase_count_and_criterion_conflicts():
    row = field('bone_lesions')
    decisions = [{'fields': ['bone_lesions'], 'owner': '#1228',
                  'decision': 'Retain count and >2 comparator separately from presence.'}]
    implementation_contracts([row], {}, [], {}, decisions)
    assert row['disposition'] == 'requires_structured_representation'
    assert 'count and >2 comparator' in row['reason']
    assert row['implementation_contract']['semantic_approval'] is False


def test_reference_catalogs_and_unused_constants_are_not_new_clinical_facts():
    catalog = inventory_row('promop_catalog', 'therapy_class', 'class-a', 'Class A', owner='#1230')
    unused = inventory_row('frontend_constant', 'constants.ts:OLD_OPTIONS', 'old', 'Old')
    used = inventory_row('frontend_constant', 'constants.ts:ER_OPTIONS', 'positive', 'Positive')
    frontend = {'constants': [{'name': name, 'file': 'constants.ts'} for name in ['OLD_OPTIONS', 'ER_OPTIONS']],
                'controls': [{'constant': 'ER_OPTIONS', 'field': 'estrogen_receptor_status'}]}
    result = implementation_contracts([catalog, unused, used, field('estrogen_receptor_status')], {}, [], frontend)
    assert result['unresolved_row_ids'] == []
    assert catalog['implementation_contract']['destinations'] == ['reference_tables.therapy_class']
    assert catalog['owning_issue'] == '#1230'
    assert catalog['disposition'] == 'needs_review'
    assert unused['implementation_contract']['status'] == 'retained_source_evidence'
    assert unused['retired'] is None
    assert unused['owning_issue'] == '#1231'
    assert used['implementation_contract']['destinations'] == ['estrogen_receptor_status']
    assert used['owning_issue'] == '#1227'


def test_missing_fields_reviewed_decisions_and_genomics_ownership_remain_explicit():
    row = field('genomics_tp53')
    row['existing_mappings'] = [{'status': 'approved', 'concept_id': 0}]
    reviewed = field('country', 'location')
    reviewed.update(disposition='verified_mapping', reviewer='existing-review', reason='Existing decision')
    before = deepcopy(reviewed)
    missing = inventory_row('cancerbot_history', 'old.py', 'legacy', 'Legacy', owner='#1228')
    missing['destination_candidates'] = ['missing_field']
    implementation_contracts([row, reviewed, missing], {}, [], {})
    assert row['owning_issue'] == '#1229'
    assert row['existing_mappings'] == [{'status': 'approved', 'concept_id': 0}]
    assert missing['implementation_contract']['missing_destinations'] == ['missing_field']
    assert missing['retired'] is None
    assert {k: v for k, v in reviewed.items() if k != 'implementation_contract'} == before


def test_source_drift_does_not_keep_previous_routing_contract():
    row = inventory_row('cancerbot_source', 'stages:literal:0', 'I', 'I')
    row['implementation_contract'] = {'destinations': ['stage'], 'route_keys': ['stagesMm']}
    row['destination_route_keys'] = ['stagesMm']
    stale_crosswalk = {'status': 'source_revision_requires_review', 'bindings': [
        {'option_list': 'stagesMm', 'status': 'source_route_recorded',
         'destination_candidates': ['stage'], 'representation': 'context_required',
         'owning_issue': '#1228'}]}
    result = implementation_contracts([row], stale_crosswalk, [], {})
    assert row['id'] in result['unresolved_row_ids']
    assert row['implementation_contract']['destinations'] == []
    assert row['implementation_contract']['route_keys'] == []
