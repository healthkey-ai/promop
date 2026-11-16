"""The focused #1311 evidence must match the inventory it is extracted from."""
import json
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command

from omop_core.services.field_inventory_priority import (
    priority_json, priority_markdown, priority_ready,
)

DOCS = Path(__file__).resolve().parent.parent / 'docs' / 'field-mapping-inventory'
MANIFEST = DOCS / 'manifest.json'
JSON_ARTIFACT = DOCS / 'genomics-priority-coverage.json'
MARKDOWN_ARTIFACT = DOCS / 'genomics-priority-coverage.md'


def coverage():
    return json.loads(MANIFEST.read_text())['priority_field_coverage']


def test_committed_artifacts_match_the_inventory_section():
    section = coverage()

    assert JSON_ARTIFACT.read_text() == priority_json(section)
    assert MARKDOWN_ARTIFACT.read_text() == priority_markdown(section)
    assert section['total_fields'] == 42
    assert section['disease_memberships'] == 51
    assert priority_ready(section) is True


@pytest.mark.django_db
def test_write_and_check_round_trip(tmp_path):
    call_command('export_genomics_priority_coverage', output_dir=str(tmp_path), write=True)
    json_path = tmp_path / 'genomics-priority-coverage.json'
    markdown_path = tmp_path / 'genomics-priority-coverage.md'
    assert json_path.read_text() == JSON_ARTIFACT.read_text()
    assert markdown_path.read_text() == MARKDOWN_ARTIFACT.read_text()

    call_command('export_genomics_priority_coverage', output_dir=str(tmp_path), check=True)

    markdown_path.write_text('stale')
    with pytest.raises(CommandError, match='stale'):
        call_command('export_genomics_priority_coverage', output_dir=str(tmp_path), check=True)


def _minimal_coverage(state, problems):
    return {
        'owning_issue': '#1311',
        'total_fields': 1,
        'disease_memberships': 1,
        'counts': {state: 1},
        'by_disease': {'BC': {'field_names': ['genomics_brca1'], 'counts': {state: 1}}},
        'fields': [{
            'field_name': 'genomics_brca1', 'marker_key': 'brca1', 'gene': 'BRCA1',
            'kind': 'gene', 'diseases': ['BC'], 'legacy_keys': [],
            'storage_mapping': {'status': 'approved', 'omop_table': 'measurement',
                                'value_kind': 'json', 'multiple': True,
                                'source_value': 'genomics:brca1', 'concept_code': '81252-9'},
            'problems': problems, 'state': state, 'declared_concept_evidence': [],
            'compatible_parent_candidate_ids': [], 'semantic_approval': False,
        }],
        'limitation': 'Storage dispositions are not clinical approval.',
    }


@pytest.mark.django_db
@pytest.mark.parametrize('state,passes', [
    ('source_only_parent', True),
    ('standard_parent_candidate_requires_review', True),
    ('storage_recipe_incomplete', False),
])
def test_command_gate_follows_the_storage_disposition(tmp_path, state, passes):
    """Missing/unapproved/incompatible fail; source-only and candidates pass."""
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({
        'priority_field_coverage': _minimal_coverage(state, problems=(
            [] if passes else ['unapproved_mapping'])),
    }))

    if passes:
        call_command('export_genomics_priority_coverage', manifest=str(manifest),
                     output_dir=str(tmp_path), write=True)
    else:
        with pytest.raises(CommandError, match='incomplete'):
            call_command('export_genomics_priority_coverage', manifest=str(manifest),
                         output_dir=str(tmp_path), write=True)
