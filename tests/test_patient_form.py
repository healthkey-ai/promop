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


def test_crab_navigation_uses_readable_canonical_clearance_without_lifting_gate():
    inputs = {'serum_calcium_mg_dl', 'creatinine_clearance_ml_min',
              'serum_creatinine_mg_dl', 'hemoglobin_g_dl', 'bone_lesions'}
    descriptors = patient_form_descriptors({
        name: {'kind': 'direct', 'writable': True, 'target': 'patient_record'}
        for name in inputs
    }, readable_fields=inputs | {'meets_crab'})
    crab = descriptors['meets_crab']
    assert set(crab['inputs']) == inputs
    assert all(name in descriptors for name in crab['inputs'])
    assert crab['writable'] is False
    assert 'clinical review' in crab['compatibility_reason']


def test_mcl_dependency_graph_resolves_without_exposing_host_withheld_age():
    inputs = {'date_of_birth', 'ecog_performance_status', 'ldh_u_l',
              'ldh_upper_limit_normal', 'wbc_count_thousand_per_ul',
              'ki67_proliferation_index'}
    descriptors = patient_form_descriptors({
        name: {'kind': 'direct', 'writable': True, 'target': 'patient_record'}
        for name in inputs | {'patient_age'}
    }, readable_fields=inputs)
    assert 'patient_age' not in descriptors
    for root in ('mipi_risk', 'mipi_c_risk', 'high_risk_mcl_criteria'):
        pending = [(root, set())]
        while pending:
            name, ancestors = pending.pop()
            assert name not in ancestors
            assert name in descriptors
            entry = descriptors[name]
            if entry.get('inputs'):
                assert entry['writable'] is False
                assert entry['compatibility_reason']
                pending.extend((child, ancestors | {name}) for child in entry['inputs'])
    for name in ('mipi_risk', 'mipi_c_risk'):
        assert 'date_of_birth' in descriptors[name]['inputs']
        assert 'ldh_upper_limit_normal' in descriptors[name]['inputs']


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
