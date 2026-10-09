import copy
import hashlib
import json
from pathlib import Path

import pytest

from omop_core.services import field_inventory_crosswalk as crosswalk
from omop_core.services.field_inventory import inventory_row


def source(tmp_path, monkeypatch):
    path = tmp_path / 'provider.py'
    path.write_text("raise RuntimeError('Do not execute source')")
    monkeypatch.setattr(crosswalk, 'SOURCES', {'provider.py': hashlib.sha256(path.read_bytes()).hexdigest()})
    return path


def test_all_current_public_bindings_have_explicit_routes():
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'docs/field-mapping-inventory/manifest.json').read_text())
    assert set(crosswalk.reviewed_routes()) == {b['option_list'] for b in manifest['cancerbot_bindings']}


def test_ancestry_and_prior_line_categories_keep_their_representation_conflicts():
    routes = crosswalk.reviewed_routes()
    assert routes['ethnicity']['representation'] == 'semantic_conflict'
    assert routes['ethnicity']['destination_candidates'] == ['ethnicity', 'race']
    assert routes['priorTherapy']['representation'] == 'structured'
    assert routes['priorTherapy']['owning_issue'] == '#1230'
    assert routes['estrogenReceptorStatus']['owning_issue'] == '#1227'


def test_routes_preserve_typed_source_scope_candidates_and_approval(tmp_path, monkeypatch):
    source(tmp_path, monkeypatch)
    names = ['plannedTherapiesFirstLineMm', 'plannedTherapiesSecondLineBc', 'gelfCriteriaStatusFl']
    rows = [inventory_row('cancerbot_live', name, '', 'Unknown', value='', scope={'parent_keys': ['gene-1']}) for name in names]
    rows += [inventory_row('promop_field', 'PatientRecord', name, name, field=name, kind='field')
             for name in ['planned_therapies', 'gelf_criteria_options']]
    before = copy.deepcopy(rows)
    bindings = [{'option_list': name} for name in names]
    result = crosswalk.reconcile_cancerbot_destinations(tmp_path, bindings, rows, {})
    assert result['counts'] == {'source_route_recorded': 3}
    assert result['bindings'][0]['context'] == {'disease': 'MM', 'line': 'FirstLine', 'treatment_status': 'planned'}
    assert result['bindings'][1]['context']['disease'] == 'BC'
    assert result['bindings'][2]['representation'] == 'semantic_conflict'
    for old, new in zip(before, rows):
        assert old == {k: v for k, v in new.items() if k != 'destination_route_keys'}
    crosswalk.reconcile_cancerbot_destinations(tmp_path, bindings, rows, {})
    assert rows[0]['destination_route_keys'] == [names[0]]


@pytest.mark.parametrize('change', ['drift', 'missing'])
def test_source_change_clears_old_route_evidence(tmp_path, monkeypatch, change):
    path = source(tmp_path, monkeypatch)
    rows = [inventory_row('cancerbot_live', 'gender', 'F', 'Female', value='F')]
    bindings = [{'option_list': 'gender'}]
    result = crosswalk.reconcile_cancerbot_destinations(tmp_path, bindings, rows, {})
    assert result['bindings'][0]['status'] == 'destination_missing'
    if change == 'drift':
        path.write_text('changed routing')
    else:
        path.unlink()
    result = crosswalk.reconcile_cancerbot_destinations(tmp_path, bindings, rows, {})
    assert result['status'] == 'source_unavailable_or_changed'
    assert 'destination_route_keys' not in rows[0]


def test_new_binding_missing_destination_and_shared_literal_remain_explicit(tmp_path, monkeypatch):
    source(tmp_path, monkeypatch)
    row = inventory_row('cancerbot_source', 'stages:literal:0', 'I', 'I', value='I')
    bindings = [{'option_list': name, 'source_row_ids': [row['id']]} for name in ['stagesMm', 'stagesCll', 'futureList']]
    result = crosswalk.reconcile_cancerbot_destinations(tmp_path, bindings, [row], {})
    assert result['counts'] == {'destination_missing': 2, 'unreviewed_binding': 1}
    assert row['destination_route_keys'] == ['stagesMm', 'stagesCll']
    assert {r['context']['disease'] for r in result['bindings'][:2]} == {'MM', 'CLL'}
    assert all(r['context']['system'] is None for r in result['bindings'][:2])
    assert row['destination_path'] is None
    assert row['disposition'] == 'needs_review'
