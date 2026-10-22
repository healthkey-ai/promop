"""Portable form navigation does not grant clinical write permissions."""
from omop_core.services.patient_form import patient_form_descriptors


def test_computed_inputs_are_navigation_not_writable_verdicts():
    descriptors = patient_form_descriptors({
        'height': {'kind': 'direct', 'writable': True, 'target': 'patient_record'},
        'weight': {'kind': 'direct', 'writable': True, 'target': 'patient_record'},
    })
    assert descriptors['bmi']['inputs'] == ['weight', 'height']
    assert descriptors['bmi']['writable'] is False
    assert descriptors['height']['writable'] is True
    assert descriptors['mipi_c_risk']['inputs'][-1] == 'ki67_proliferation_index'
    assert descriptors['high_risk_mcl_criteria']['inputs'][:2] == ['mipi_risk', 'mipi_c_risk']


def test_host_read_surface_and_caller_write_gate_are_independent():
    descriptors = patient_form_descriptors({
        'weight': {'kind': 'direct', 'writable': True, 'target': 'patient_record'},
        'validated': {'kind': 'direct', 'writable': True, 'target': 'patient_record'},
    }, readable_fields={'weight', 'bmi'}, writable=False)
    assert 'validated' not in descriptors
    assert descriptors['weight']['writable'] is False
    assert descriptors['weight']['read_only_for_caller'] is True
    assert descriptors['bmi']['inputs'] == ['weight', 'height']


def test_slim_compatibility_gate_is_explicit_without_a_new_formula():
    descriptors = patient_form_descriptors({})
    assert descriptors['meets_slim']['inputs'] == ['clonal_plasma_cells', 'kappa_flc', 'lambda_flc', 'bone_lesions']
    assert 'clinical review' in descriptors['meets_slim']['compatibility_reason']
    assert descriptors['meets_slim']['writable'] is False
    assert 'meetsSLIM' in descriptors['meets_slim']['bindings']


def test_unsupported_projectors_and_special_assertions_are_not_advertised():
    descriptor = patient_form_descriptors({
        'a': {'kind': 'direct', 'writable': True, 'target': 'patient_record',
              'projection': {'omop_table': 'condition', 'concept_id': 1}},
        'relapse_count': {'kind': 'direct', 'writable': True, 'target': 'patient_record',
                          'projection': {'omop_table': 'observation', 'concept_id': 0}},
        'genomics_test': {'kind': 'editable', 'writable': True, 'target': 'patient_record'},
    })
    assert not descriptor['a']['writable']
    assert not descriptor['relapse_count']['writable']
    assert not descriptor['genomics_test']['writable']
