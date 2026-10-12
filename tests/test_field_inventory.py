from datetime import date

import pytest

from omop_core.services.field_inventory import (
    cancerbot_source, coverage, inventory_row, screen_candidate, validate_live_export,
)


def test_identity_keeps_types_scope_and_mutable_label_separate():
    def row(value, **kwargs):
        return inventory_row('test', 'answers', value, 'label', value=value, **kwargs)
    values = [True, 1, '1', False, 0, '', None]
    rows = [row(value) for value in values]
    assert len({r['id'] for r in rows}) == len(values)
    assert row('T1', scope={'basis': 'c'})['id'] != row('T1', scope={'basis': 'p'})['id']
    original = row('code')
    renamed = inventory_row('test', 'answers', 'code', 'new label', value='code')
    assert original['id'] == renamed['id']


def test_coverage_keeps_unresolved_duplicate_and_structured_rows():
    rows = [inventory_row('test', 'answers', '', 'Unknown'),
            inventory_row('test', 'answers', 'none', 'None', disposition='requires_structured_representation')]
    result = coverage(rows + rows[:1], {})
    assert result['total_rows'] == sum(result['by_disposition'].values()) == 3
    assert result['without_candidate'] == 3
    assert result['without_destination'] == 3
    assert result['duplicate_source_ids'] == [rows[0]['id']]
    assert coverage(list(reversed(rows + rows[:1])), {}) == result


def test_static_scan_keeps_literals_inside_dynamic_lists_without_execution(tmp_path):
    source = tmp_path / 'options.py'
    source.write_text('''
raise RuntimeError("Never execute this file")
class ValueOptions:
    def bone_lesions(self):
        return {'': 'Unknown', 1: 'One', **database_options()}
    def get_all_options(self):
        return {'boneLesions': {'options': self.bone_lesions()}}
''')
    data = cancerbot_source(source)
    assert len(data['rows']) == 2
    assert data['bindings'][0]['option_list'] == 'boneLesions'
    assert data['bindings'][0]['coverage'] == 'requires_live_export'
    assert {r['canonical_value']['type'] for r in data['rows']} == {'string', 'integer'}


def test_complete_literal_list_does_not_require_a_live_database(tmp_path):
    source = tmp_path / 'options.py'
    source.write_text('''
class ValueOptions:
    def bone_lesions(self):
        return {'': 'Unknown', '1': '1', '2': '2', 'more than 2': 'More than 2'}
    def get_all_options(self):
        return {'boneLesions': {'options': self.to_value_and_label(self.bone_lesions)}}
''')
    data = cancerbot_source(source)
    assert data['bindings'][0]['coverage'] == 'covered_by_static_source'
    assert set(data['bindings'][0]['source_row_ids']) == {r['id'] for r in data['rows']}


@pytest.mark.parametrize('expression', [
    'self.filter_using_database(self.answers)',
    'self.to_value_and_label(self.filter_using_database(self.answers))',
    'self.answers if self.enabled else self.database_answers',
    'self.answers | self.database_answers',
    'self.to_value_and_label(self.answers, filter=self.current_disease)',
])
def test_dynamic_use_of_literal_list_stays_unresolved(tmp_path, expression):
    source = tmp_path / 'options.py'
    source.write_text(f'''class ValueOptions:
    def answers(self):
        return {{'positive': 'Positive', 'negative': 'Negative'}}
    def get_all_options(self):
        return {{'filteredAnswers': {{'options': {expression}}}}}
''')
    data = cancerbot_source(source)
    assert data['bindings'][0]['coverage'] == 'requires_live_export'
    assert 'source_row_ids' not in data['bindings'][0]
    assert len(data['rows']) == 2  # Keep source evidence without asserting membership.


@pytest.mark.parametrize('expression', ['self.answers', 'self.answers()', 'self.to_value_and_label(self.answers())'])
def test_direct_literal_binding_forms(expression):
    from omop_core.services.field_inventory import literal_binding_method
    assert literal_binding_method("{'options': " + expression + '}', {'answers'}) == 'answers'


def test_live_export_keeps_nested_gene_context_and_reports_missing_lists():
    payload = {'schema_version': 1, 'source_revision': 'abc', 'exported_at': '2026-09-14',
               'options': {'variants': {'BRCA1': {'options': [{'value': 'unknown', 'label': 'Unknown'}]},
                                        'BRCA2': {'options': [{'value': 'unknown', 'label': 'Unknown'}]}}}}
    rows, missing = validate_live_export(payload, ['variants', 'origins'])
    assert missing == ['origins']
    assert len({r['id'] for r in rows}) == 2
    assert rows[0]['scope']['parent_keys'] == ['BRCA1']
    payload['patient_id'] = 123
    with pytest.raises(ValueError, match='only'):
        validate_live_export(payload, ['variants'])


@pytest.mark.parametrize('bad', [
    {'value': 'a', 'label': 'A', 'patient_id': 1}, {'value': {}, 'label': 'A'},
])
def test_live_export_rejects_non_reference_option_shape(bad):
    payload = {'schema_version': 1, 'source_revision': 'abc', 'exported_at': '2026-09-14',
               'options': {'variants': {'options': [bad]}}}
    with pytest.raises(ValueError):
        validate_live_export(payload, ['variants'])


def test_standard_flag_does_not_override_dates_or_synthetic_provenance():
    concept = {'concept_id': 9191, 'concept_code': '10828004', 'standard_concept': 'S',
               'invalid_reason': None, 'valid_start_date': '2000-01-01', 'valid_end_date': '2099-12-31',
               'source': None}
    vocabulary = {'vocabulary_version': 'SNOMED CT (synthetic, benchmark seed)', 'is_deprecated': False}
    result = screen_candidate(concept, vocabulary, date(2026, 9, 14))
    assert not result['passes_mechanical_screen']
    assert not result['semantic_approval']
    assert not result['provenance_verified']
    vocabulary['vocabulary_version'] = '20260101'
    concept['valid_end_date'] = '2025-01-01'
    assert not screen_candidate(concept, vocabulary, date(2026, 9, 14))['checks']['valid_dates']
    concept['valid_end_date'] = '2099-01-01'
    assert screen_candidate(concept, vocabulary, date(2026, 9, 14))['passes_mechanical_screen']
    concept['concept_id'] = 2_000_000_001
    assert not screen_candidate(concept, vocabulary, date(2026, 9, 14))['checks']['external_number_range']


def test_reference_column_allowlists_exclude_identity_and_notes():
    from omop_core.management.commands.export_field_mapping_inventory import REFERENCE_COLUMNS, RELEASE_COLUMNS
    assert not {'reviewer_id', 'created_by_id', 'notes', 'person_id', 'patient_id', 'email'} & (REFERENCE_COLUMNS | RELEASE_COLUMNS)


def test_manifest_conservation_and_missing_references():
    from omop_core.services.field_inventory import validate_manifest
    row = inventory_row('test', 'answers', 'unknown', 'Unknown')
    manifest = {'rows': [row], 'totals': coverage([row], {}), 'candidates': {}, 'maps_to': []}
    validate_manifest(manifest)
    manifest['totals']['total_rows'] = 0
    with pytest.raises(ValueError, match='totals'):
        validate_manifest(manifest)
    row['candidate_ids'] = [123]
    manifest['totals'] = coverage([row], {})
    with pytest.raises(ValueError, match='Candidate reference'):
        validate_manifest(manifest)


def test_typescript_parser_preserves_literals_and_flags_dynamic_options(tmp_path):
    import json
    from pathlib import Path
    import subprocess
    root = Path(__file__).resolve().parent.parent
    deps = root / 'frontend/node_modules'
    if not (deps / 'typescript').exists():
        pytest.skip('Frontend TypeScript dependency required for source parser integration test')
    source_dir = tmp_path / 'frontend/src/components/PatientInfo'
    source_dir.mkdir(parents=True)
    (tmp_path / 'frontend/node_modules').symlink_to(deps)
    (source_dir / 'Example.tsx').write_text('''
throw new Error("never execute source");
const MIXED_OPTIONS = [true, 1, '1', null, ''];
const DYNAMIC_OPTIONS = loadFromApi();
function Example() {
  return <ClinicalField name="marker" type="select" options={MIXED_OPTIONS} />;
}
''')
    result = json.loads(subprocess.check_output(['node', str(root / 'scripts/inventory-frontend-options.cjs'), str(tmp_path)], text=True))
    assert result['constants'][0]['values'] == [True, 1, '1', None, '']
    assert result['controls'][0]['field'] == 'marker'
    assert result['controls'][0]['component'] == 'Example'
    assert result['unresolved'][0]['name'] == 'DYNAMIC_OPTIONS'


def test_checked_in_snapshot_reconciles_and_grants_no_approvals():
    import json
    from pathlib import Path
    from omop_core.services.field_inventory import validate_manifest
    manifest = json.loads((Path(__file__).resolve().parent.parent /
                           'docs/field-mapping-inventory/manifest.json').read_text())
    validate_manifest(manifest)
    assert manifest['complete'] is False
    assert not manifest['totals']['duplicate_source_ids']
    assert all(row['disposition'] != 'verified_mapping' for row in manifest['rows'])


def test_staging_therapy_adapter_uses_links_and_keeps_planned_context_distinct():
    from omop_core.services.field_inventory import staging_therapy_coverage
    tables = {
        'vocabulary_disease': [{'id': 1, 'code': 'C3242'}, {'id': 2, 'code': 'C9335'}],
        'therapy_round': [{'id': 1, 'code': 'first_line_therapy'}, {'id': 2, 'code': 'second_line_therapy'}],
        'therapy_regimen': [{'id': 1}, {'id': 2}],
        'therapy_component': [{'id': 10}, {'id': 20}], 'therapy_class': [{'id': 100}, {'id': 200}],
        'disease_therapy_regimen': [
            {'id': 1, 'disease_id': 1, 'regimen_id': 1, 'round_id': 1},
            {'id': 2, 'disease_id': 2, 'regimen_id': 2, 'round_id': 2}],
        'therapy_regimen_component': [{'regimen_id': 1, 'component_id': 10}, {'regimen_id': 2, 'component_id': 20}],
        'therapy_component_class': [{'component_id': 10, 'therapy_class_id': 100}, {'component_id': 20, 'therapy_class_id': 200}],
    }
    bindings = [{'option_list': name, 'coverage': 'requires_live_export'} for name in [
        'therapiesAll', 'therapiesFirstLineMm', 'therapiesSecondLineMm', 'therapyComponentsMm',
        'therapyTypesMm', 'plannedTherapiesMm', 'mutationGenes']]
    result = staging_therapy_coverage(tables, bindings)
    members = {r['option_list']: r for r in result['memberships']}
    assert members['therapiesAll']['source_ids'] == [1, 2]
    assert members['therapiesFirstLineMm']['source_ids'] == [1]
    assert members['therapiesSecondLineMm']['source_ids'] == []
    assert members['therapyComponentsMm']['source_ids'] == [10]
    assert members['therapyTypesMm']['source_ids'] == [100]
    assert result['context_pending_lists'] == ['plannedTherapiesMm']
    assert bindings[-1]['coverage'] == 'requires_live_export'
    assert not result['orphan_disease_round_links']


def test_inventory_live_export_updates_bindings_provider_totals_and_pending_context(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from omop_core.management.commands import export_field_mapping_inventory as command

    source = {'rows': [], 'bindings': [
        {'option_list': 'answers', 'coverage': 'requires_live_export'},
        {'option_list': 'emptyAnswers', 'coverage': 'requires_live_export'},
        {'option_list': 'missingAnswers', 'coverage': 'requires_live_export'},
        {'option_list': 'plannedTherapiesMm', 'coverage': 'staging_catalog_available_context_pending'},
    ]}
    payload = {'schema_version': 1, 'source_revision': 'live-revision', 'exported_at': '2026-09-14T00:00:00Z',
               'options': {'answers': {'options': [{'value': 'x', 'label': 'X'}]},
                           'emptyAnswers': {'options': []},
                           'plannedTherapiesMm': {'options': [{'value': 'regimen', 'label': 'Regimen'}]}}}
    live = tmp_path / 'live.json'
    live.write_text(json.dumps(payload))
    monkeypatch.setattr(command, 'collect_reference_tables', lambda: ({}, []))
    monkeypatch.setattr(command.models.Vocabulary.objects, 'values', lambda *args: [])
    monkeypatch.setattr(command, 'collect_rows', lambda *args: [])
    monkeypatch.setattr(command, 'collect_descriptor_options', lambda *args: ([], {'status': 'base_descriptors_captured'}))
    monkeypatch.setattr(command, 'cancerbot_source', lambda *args: source)
    monkeypatch.setattr(command, 'staging_therapy_coverage', lambda *args: {'context_pending_lists': ['plannedTherapiesMm']})
    monkeypatch.setattr(command, 'attach_candidates', lambda *args: {})
    monkeypatch.setattr(command, 'attach_relationships', lambda *args: [])
    monkeypatch.setattr(command.connection.introspection, 'table_names', lambda: [])
    monkeypatch.setattr(command, 'read_table', lambda *args: [])
    monkeypatch.setattr(command, 'source_revision', lambda *args: {})
    result = command.build_inventory(Path(__file__).resolve().parent.parent, tmp_path,
        {'files': {}, 'constants': [], 'controls': [], 'unresolved': []}, live)
    coverage_data = result['totals']['source_coverage']['cancerbot_public_lists']
    assert coverage_data['missing_live_lists'] == ['missingAnswers']
    assert coverage_data['by_provider'] == {'covered_by_live_export': 3, 'requires_live_export': 1}
    assert result['therapy_source_coverage']['context_pending_lists'] == []
    bindings = {b['option_list']: b for b in result['cancerbot_bindings']}
    assert bindings['emptyAnswers']['live_source_row_ids'] == []
    assert bindings['answers']['live_source_revision'] == 'live-revision'
    live_ids = {r['id'] for r in result['rows'] if r['source'] == 'cancerbot_live'}
    assert live_ids == {i for b in bindings.values() for i in b.get('live_source_row_ids', [])}
    command.validate_manifest(result)
