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
    assert descriptors['meets_slim']['compatibility_scope'] == 'result'
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
    assert crab['compatibility_scope'] == 'result'
    assert 'clinical review' in crab['compatibility_reason']


def test_adequacy_subforms_are_navigable_but_results_are_gated():
    inputs = {
        'renal_adequacy_status': {'date_of_birth', 'gender', 'serum_creatinine_mg_dl',
                                  'creatinine_clearance_ml_min'},
        'hepatic_adequacy_status': {'ast_u_l', 'alt_u_l', 'bilirubin_total_mg_dl',
                                    'ethnicity', 'gender'},
        'haematological_adequacy_status': {'anc_thousand_per_ul',
                                           'platelet_count_thousand_per_ul', 'hemoglobin_g_dl'},
    }
    source = {name for names in inputs.values() for name in names}
    descriptors = patient_form_descriptors({
        name: {'kind': 'direct', 'writable': True, 'target': 'patient_record'}
        for name in source
    }, readable_fields=source | set(inputs))
    for root, expected in inputs.items():
        entry = descriptors[root]
        assert set(entry['inputs']) == expected
        assert entry['kind'] == 'computed'
        assert entry['writable'] is False
        assert entry['compatibility_scope'] == 'result'
        assert all(name in descriptors for name in entry['inputs'])


def test_mcl_navigation_names_the_source_facts_that_exist():
    descriptors = patient_form_descriptors({})
    mcl = descriptors['high_risk_mcl_criteria']
    for name in ('cytogenetic_markers', 'largest_lymph_node_size', 'spleen_size',
                 'alc_thousand_per_ul'):
        assert name in mcl['inputs']
    assert mcl['writable'] is False
    assert mcl['compatibility_scope'] == 'result'


def test_mcl_dependency_graph_resolves_without_exposing_host_withheld_age():
    inputs = {'date_of_birth', 'ecog_performance_status', 'ldh_u_l',
              'ldh_upper_limit_normal', 'wbc_count_thousand_per_ul',
              'ki67_proliferation_index', 'cytogenetic_markers',
              'largest_lymph_node_size', 'spleen_size', 'alc_thousand_per_ul'}
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


def test_crab_verdict_is_three_valued_from_the_same_inputs_the_form_edits():
    from omop_core.models import PatientRecord
    from omop_core.services.patient_record_service import derive_meets_crab

    record = PatientRecord()
    # One positive component is enough even when the rest are unknown.
    record.hemoglobin_g_dl = '8.4'
    assert derive_meets_crab(record) is True

    # A negative answer requires the whole work-up; a missing component keeps
    # the result unknown instead of asserting "not met".
    record.hemoglobin_g_dl = '13.5'
    assert derive_meets_crab(record) is None

    record.serum_calcium_mg_dl = '9.4'
    record.creatinine_clearance_ml_min = '92'
    record.bone_lesions = 'Absent'
    assert derive_meets_crab(record) is False

    # Serum creatinine substitutes for clearance only when clearance is absent.
    record.creatinine_clearance_ml_min = None
    record.serum_creatinine_mg_dl = '2.3'
    assert derive_meets_crab(record) is True


def test_slim_verdict_keeps_the_involved_chain_burden_guard():
    from omop_core.models import PatientRecord
    from omop_core.services.patient_record_service import derive_meets_slim

    record = PatientRecord(clonal_plasma_cells='70')
    assert derive_meets_slim(record) is True

    record.clonal_plasma_cells = '10'
    # Ratio 125 with both chains far below the involved-chain floor is not the
    # SLiM light-chain criterion, which is PROMOP's documented existing rule.
    record.kappa_flc = '50'
    record.lambda_flc = '0.4'
    assert derive_meets_slim(record) is False

    record.kappa_flc = '300'
    record.lambda_flc = '2'
    assert derive_meets_slim(record) is True

    record.kappa_flc = None
    assert derive_meets_slim(record) is None
