import copy
import hashlib
import json
from pathlib import Path

import pytest

from omop_core.services.cancerbot_static_options import StaticOptions, Unresolved
from omop_core.services.field_inventory import cancerbot_source, validate_manifest


SOURCE = '''
raise RuntimeError("Do not execute source")
class ValueOptions:
    _DISEASES = {'FL'}
    @staticmethod
    def to_value_and_label(data):
        return [{'value': k, 'label': v} for k, v in data.items()]
    @property
    def answers(self):
        return {'': 'Unknown', 'age': 'Age criterion', 'none': 'None'}
    def scoped(self, disease_code):
        if disease_code.upper() in self._DISEASES:
            return self.answers
        return {'': self.answers['']}
    def empty(self):
        return {}
    @property
    def prior(self):
        values = ['One', 'Two']
        mapped = {x: x for x in values}
        return {'': 'Unknown', **mapped}
    @property
    def live(self):
        from trials.models import MutationGene
        raise RuntimeError("Do not import or query")
    def recursive(self):
        return self.recursive()
    def get_all_options(self):
        return {
            'fl': {'options': self.to_value_and_label(self.scoped('FL'))},
            'mm': {'options': self.to_value_and_label(self.scoped('MM'))},
            'empty': {'options': self.to_value_and_label(self.empty())},
            'prior': {'options': self.to_value_and_label(self.prior)},
            'live': {'options': self.to_value_and_label(self.live)},
            'recursive': {'options': self.to_value_and_label(self.recursive())},
        }
'''


def test_deterministic_expansion_preserves_blank_empty_and_disease_context(tmp_path):
    source = tmp_path / 'options.py'
    source.write_text(SOURCE)
    result = cancerbot_source(source)
    bindings = {b['option_list']: b for b in result['bindings']}
    rows = {r['id']: r for r in result['rows']}
    assert bindings['fl']['option_count'] == 3
    assert bindings['mm']['option_count'] == 1
    assert bindings['mm']['source_context'] == {'disease': 'MM'}
    assert rows[bindings['mm']['source_row_ids'][0]]['canonical_value'] == {'type': 'string', 'value': ''}
    # Direct empty literals were already covered by the original extractor.
    assert bindings['empty']['coverage'] == 'covered_by_static_source'
    assert bindings['empty']['source_row_ids'] == []
    assert bindings['prior']['option_count'] == 3
    assert bindings['live']['coverage'] == 'requires_live_export'
    assert bindings['live']['static_resolution']['provider_models'] == ['MutationGene']
    assert bindings['recursive']['coverage'] == 'requires_live_export'
    assert all(r['disposition'] == 'needs_review' for r in result['rows'])


@pytest.mark.parametrize('expression', [
    "__import__('os').system('echo should-never-run')",
    "self.answers if self.unknown_flag else {}",
    "self.recursive()", "self.scoped", "self.live",
    "self.answers | self.live",
])
def test_unsupported_or_dynamic_source_is_not_executed_or_claimed_complete(expression):
    interpreter = StaticOptions(SOURCE)
    with pytest.raises(Unresolved):
        interpreter.resolve("{'options': self.to_value_and_label(" + expression + ')}')


def test_bound_prevents_unbounded_recursive_or_comprehension_work():
    source = SOURCE.replace("values = ['One', 'Two']", "values = " + repr(list(range(12000))))
    with pytest.raises(Unresolved, match='budget'):
        StaticOptions(source).resolve("{'options': self.to_value_and_label(self.prior)}")


def test_trial_search_exclusion_requires_matching_provider_evidence(tmp_path):
    source = tmp_path / 'options.py'
    source.write_text('''class ValueOptions:
    @property
    def registers(self):
        from trials.models import Trial
        return {'': 'All', **database_rows()}
    @staticmethod
    def to_value_and_label(data):
        return [{'value': k, 'label': v} for k, v in data.items()]
    def get_all_options(self):
        return {'register': {'options': self.to_value_and_label(self.registers)}}
''')
    result = cancerbot_source(source)
    assert result['bindings'][0]['coverage'] == 'excluded_trial_search'
    assert result['rows'][0]['disposition'] == 'not_applicable'
    source.write_text(source.read_text().replace('import Trial', 'import MutationGene'))
    assert cancerbot_source(source)['bindings'][0]['coverage'] == 'requires_live_export'


def test_saved_snapshot_reconciliation_is_idempotent_and_preserves_reference_evidence(tmp_path):
    from omop_core.management.commands.reconcile_field_inventory_sources import reconcile_sources

    manifest = json.loads(Path('docs/field-mapping-inventory/manifest.json').read_text())
    # Use a small source fixture with the same public identities in the snapshot.
    path = tmp_path / 'trials/services/value_options.py'
    path.parent.mkdir(parents=True)
    path.write_text(SOURCE)
    manifest['source_revisions']['cancerbot']['files']['trials/services/value_options.py'] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest['cancerbot_bindings'] = cancerbot_source(path)['bindings']
    manifest['totals']['source_coverage']['cancerbot_public_lists']['live_metadata'] = None
    original = copy.deepcopy(manifest)
    once = reconcile_sources(manifest, tmp_path)
    twice = reconcile_sources(copy.deepcopy(once), tmp_path)
    assert once == twice
    assert once['candidates'] == original['candidates']
    assert once['reference_tables'] == original['reference_tables']
    assert once['generated_at'] == original['generated_at']
    assert not once['complete']
    validate_manifest(once)
    path.write_text(SOURCE + '\n# changed source')
    with pytest.raises(ValueError, match='differs'):
        reconcile_sources(once, tmp_path)


@pytest.mark.parametrize('corruption', ['missing_row', 'provider_total', 'missing_list'])
def test_manifest_rejects_inconsistent_public_list_evidence(corruption):
    manifest = json.loads(Path('docs/field-mapping-inventory/manifest.json').read_text())
    if corruption == 'missing_row':
        manifest['cancerbot_bindings'][0]['source_row_ids'] = ['not-in-manifest']
    elif corruption == 'provider_total':
        manifest['totals']['source_coverage']['cancerbot_public_lists']['by_provider']['requires_live_export'] = 999
    else:
        manifest['totals']['source_coverage']['cancerbot_public_lists']['missing_live_lists'] = ['invented_missing_list']
    with pytest.raises(ValueError, match='CancerBot'):
        validate_manifest(manifest)
