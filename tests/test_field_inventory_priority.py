from copy import deepcopy

import pytest

from omop_core.services.field_inventory import inventory_row
from omop_core.services.field_inventory_priority import priority_field_coverage


def fixture():
    marker = {'key': 'tp53', 'field_name': 'genomics_tp53', 'gene': 'TP53', 'kind': 'gene', 'diseases': ['BC', 'MM', 'MCL', 'CLL']}
    row = inventory_row('promop_field', 'PatientRecord', 'genomics_tp53', 'TP53', field='genomics_tp53', kind='field')
    row['existing_mappings'] = [{'status': 'approved', 'omop_table': 'measurement', 'value_kind': 'json', 'multiple': True,
                                 'source_value': 'reviewed:tp53', 'concept_id': 0, 'vocabulary_id': 'LOINC', 'concept_code': '81252-9'}]
    candidate = {'concept_id': 123, 'vocabulary_id': 'LOINC', 'concept_code': '81252-9', 'domain_id': 'Observation',
                 'passes_mechanical_screen': True, 'provenance_verified': False}
    return marker, row, candidate


def test_shared_field_has_every_disease_membership_without_false_standard_coverage():
    marker, row, candidate = fixture()
    original = deepcopy(row)
    result = priority_field_coverage([marker], [row], {'123': candidate})
    assert result['total_fields'] == 1
    assert result['disease_memberships'] == 4
    assert set(result['by_disease']) == {'BC', 'MM', 'MCL', 'CLL'}
    assert result['counts'] == {'source_only_parent': 1}
    assert result['fields'][0]['problems'] == []
    assert result['fields'][0]['compatible_parent_candidate_ids'] == []
    assert {k: v for k, v in row.items() if k != 'priority_field_coverage'} == original


@pytest.mark.parametrize('changes', [{'status': 'rejected'}, {'value_kind': 'string'}, {'multiple': False},
                                     {'omop_table': 'observation'}, {'source_value': ''}])
def test_incomplete_storage_is_not_counted_as_standard_or_source_only(changes):
    marker, row, candidate = fixture()
    row['existing_mappings'][0].update(changes)
    mapping = deepcopy(row['existing_mappings'])
    result = priority_field_coverage([marker], [row], {'123': candidate})
    assert result['counts'] == {'storage_recipe_incomplete': 1}
    assert row['existing_mappings'] == mapping


def test_missing_field_and_missing_mapping_remain_explicit():
    marker, row, candidate = fixture()
    result = priority_field_coverage([marker], [], {})
    assert result['fields'][0]['problems'] == ['missing_field', 'missing_or_ambiguous_mapping']
    row['existing_mappings'] = []
    result = priority_field_coverage([marker], [row], {'123': candidate})
    assert result['counts'] == {'storage_recipe_incomplete': 1}


def test_compatible_question_candidate_does_not_approve_gene_or_variant_answers():
    marker, row, candidate = fixture()
    candidate['domain_id'] = 'Measurement'
    result = priority_field_coverage([marker], [row], {'123': candidate})
    assert result['counts'] == {'standard_parent_candidate_requires_review': 1}
    assert result['fields'][0]['compatible_parent_candidate_ids'] == [123]
    assert result['fields'][0]['semantic_approval'] is False
    candidate['passes_mechanical_screen'] = False
    assert priority_field_coverage([marker], [row], {'123': candidate})['counts'] == {'source_only_parent': 1}
