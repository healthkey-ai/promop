"""The walk counts how much LOINC axis evidence the loaded data holds (#2051)."""
import pytest

from omop_core.mapping import axis_evidence, loinc_axes
from omop_core.models import PatientSourceCode, SourceCodeConceptMapping
from tests.factories import PersonFactory

pytestmark = pytest.mark.django_db

LAB = 'ACME-LAB'


def mapping(code='GLU', **fields):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=LAB, source_code=code, omop_table='measurement', **fields)


def resource(**fields):
    return {'resourceType': 'Observation', 'code': {'coding': []}, **fields}


def bases(evidence, axis):
    return evidence[axis]['bases']


def test_units_settle_property_and_scale_and_nothing_else():
    evidence = axis_evidence.code_evidence(mapping(), ['mg/dL'], [])
    assert bases(evidence, 'property') == {loinc_axes.BASIS_UNIT}
    assert bases(evidence, 'scale_type') == {loinc_axes.BASIS_VALUE_TYPE}
    for axis in ('component', 'time_aspect', 'system', 'method_type'):
        assert not bases(evidence, axis) and not evidence[axis]['hint'] and not evidence[axis]['varies']


def test_two_property_variants_mean_the_property_varies():
    evidence = axis_evidence.code_evidence(mapping(), ['mg/dL', 'mmol/L'], [], variant_count=2)
    assert evidence['property']['varies'] and evidence['property']['hint']
    assert not bases(evidence, 'property'), 'units implying different properties settle none'


def test_specimens_across_patients_settle_the_system_and_vary_when_they_disagree():
    rows = [resource(specimen={'display': 'Serum'}), resource(specimen={'display': 'Urine'})]
    evidence = axis_evidence.code_evidence(mapping(), [], rows)
    assert bases(evidence, 'system') == {loinc_axes.BASIS_SPECIMEN}
    assert evidence['system']['varies']
    one = axis_evidence.code_evidence(mapping('B'), [], rows[:1])
    assert bases(one, 'system') == {loinc_axes.BASIS_SPECIMEN} and not one['system']['varies']


def test_method_and_period_are_hints_and_methods_that_differ_vary():
    rows = [resource(method={'text': 'Immunoassay'}, effectivePeriod={'start': '2026-01-01'}),
            resource(method={'text': 'HPLC'})]
    evidence = axis_evidence.code_evidence(mapping(), [], rows)
    assert evidence['method_type']['hint'] and evidence['method_type']['varies']
    assert evidence['time_aspect']['hint'] and not bases(evidence, 'time_aspect')
    assert not bases(evidence, 'method_type'), 'free-text method never settles the axis'


def test_a_curator_entry_counts_as_a_basis_and_a_cleared_one_does_not():
    evidence = axis_evidence.code_evidence(
        mapping(loinc_axes={'component': 'Glucose', 'system': None, 'method_type': ''}), [], [])
    assert bases(evidence, 'component') == {loinc_axes.BASIS_CURATOR}
    assert bases(evidence, 'method_type') == {loinc_axes.BASIS_CURATOR}, 'methodless is a constraint'
    assert not bases(evidence, 'system')


def test_a_quantitative_value_on_any_patient_settles_the_scale():
    evidence = axis_evidence.code_evidence(mapping(), [], [resource(), resource(valueQuantity={'value': 4.2})])
    assert bases(evidence, 'scale_type') == {loinc_axes.BASIS_VALUE_TYPE}


def test_the_summary_counts_a_code_once_per_axis_and_round_trips():
    summary = axis_evidence.Summary()
    summary.add(axis_evidence.code_evidence(mapping('A'), ['mg/dL'], [resource(specimen={'display': 'Serum'})]))
    summary.add(axis_evidence.code_evidence(mapping('B'), ['mg/dL', 'mmol/L'], [], variant_count=2))
    summary.add(axis_evidence.code_evidence(mapping('C'), [], []))
    data = summary.as_dict()
    assert data['codes'] == 3
    assert data['axes']['property'] == {'settled': 1, 'hint': 1, 'varies': 1, 'bases': {'unit': 1}}
    assert data['axes']['system'] == {'settled': 1, 'hint': 0, 'varies': 0, 'bases': {'specimen': 1}}
    assert data['axes']['component'] == {'settled': 0, 'hint': 0, 'varies': 0, 'bases': {}}
    resumed = axis_evidence.Summary(data)
    resumed.add(axis_evidence.code_evidence(mapping('D'), ['g/dL'], []))
    assert resumed.as_dict()['axes']['property'] == {'settled': 2, 'hint': 1, 'varies': 1, 'bases': {'unit': 2}}
    stale = axis_evidence.Summary({'codes': 2, 'axes': {'property': {'settled': 1, 'bases': {'bogus': 3, 'unit': 1}}}})
    assert stale.as_dict()['axes']['property']['bases'] == {'unit': 1}, 'a basis the code no longer knows is dropped'


def test_resources_are_read_per_code_in_one_query(django_assert_num_queries):
    glu, alb = mapping('GLU'), mapping('ALB')
    for code, specimen in (('GLU', 'Serum'), ('GLU', 'Urine'), ('ALB', 'Plasma')):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value=code, source_vocabulary_id=LAB, omop_table='measurement',
            source_metadata=resource(specimen={'display': specimen}))
    PatientSourceCode.objects.create(
        person=PersonFactory(), source_value='GLU', source_vocabulary_id=LAB, omop_table='measurement')
    with django_assert_num_queries(1):
        found = axis_evidence.resources_by_code([glu, alb])
    assert sorted(r['specimen']['display'] for r in found[glu.pk]) == ['Serum', 'Urine'], 'empty metadata is not a row'
    assert [r['specimen']['display'] for r in found[alb.pk]] == ['Plasma']
    assert axis_evidence.resources_by_code([]) == {}


def coding(code, display=''):
    return {'system': 'http://loinc.org', 'code': code, 'display': display}


@pytest.fixture()
def loinc_axes_table():
    from omop_core.models import LoincClass, LoincCodeClass
    LoincClass.objects.create(code='CHEM', display_name='Chemistry')
    for code, component, system in (('8716-3', 'Vital signs', '^Patient'), ('85354-9', 'Blood pressure panel', 'Arterial system'),
                                    ('2345-7', 'Glucose', 'Ser/Plas'), ('2339-0', 'Glucose', 'Bld')):
        LoincCodeClass.objects.create(loinc_num=code, loinc_class_id='CHEM', component=component, property='-', time_aspect='Pt',
                                      system=system, scale_type='Qn', method_type='')


def test_a_coding_pair_on_every_row_is_not_patients_disagreeing(loinc_axes_table):
    """Epic attaches 'Vital signs' beside every vital sign; the first known coding is the row's."""
    rows = [resource(code={'coding': [coding('85354-9'), coding('8716-3')]}) for _ in range(3)]
    evidence = axis_evidence.code_evidence(mapping('BP'), [], rows)
    assert loinc_axes.BASIS_FHIR_CODING in bases(evidence, 'component')
    assert not evidence['component']['varies']


def test_codings_that_differ_on_system_vary_the_system_not_the_component(loinc_axes_table):
    rows = [resource(code={'coding': [coding('2345-7')]}), resource(code={'coding': [coding('2339-0')]})]
    evidence = axis_evidence.code_evidence(mapping(), [], rows)
    assert not evidence['component']['varies']
    assert evidence['system']['varies']
    assert bases(evidence, 'system') == {loinc_axes.BASIS_FHIR_CODING}


def test_codings_naming_different_tests_vary_the_component(loinc_axes_table):
    rows = [resource(code={'coding': [coding('2345-7')]}), resource(code={'coding': [coding('85354-9')]})]
    assert axis_evidence.code_evidence(mapping(), [], rows)['component']['varies']


def test_resources_are_bounded_per_code_in_sql(monkeypatch):
    monkeypatch.setattr(axis_evidence, 'ROWS_PER_CODE', 2)
    glu = mapping('GLU')
    for n in range(4):
        PatientSourceCode.objects.create(
            person=PersonFactory(), source_value='GLU', source_vocabulary_id=LAB, omop_table='measurement',
            source_metadata=resource(specimen={'display': f'S{n}'}))
    assert len(axis_evidence.resources_by_code([glu])[glu.pk]) == 2
