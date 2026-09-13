from datetime import date

import pytest

from omop_core.services.breast_cancer import stage_presence
from omop_core.services.patient_record_service import _get_biomarker_data, _get_genomics_pathology_data, _get_staging_data, refresh_patient_record
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PersonFactory

pytestmark = pytest.mark.django_db


def test_er_her2_are_not_methodology_score_or_ki67():
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='85337-4', value_as_string='Positive', value_as_number=80)
    MeasurementFactory(person=person, measurement_source_value='85319-2', value_as_string='Equivocal', value_as_number=2)
    MeasurementFactory(person=person, measurement_source_value='92837-4', value_as_string='Positive')
    biomarkers = _get_biomarker_data(person)
    assert biomarkers['estrogen_receptor_status'] == 'Positive'
    assert biomarkers['her2_status'] == 'Equivocal'
    assert 'ki67_proliferation_index' not in biomarkers
    data = _get_genomics_pathology_data(person)
    assert not {'test_methodology', 'oncotype_dx_score', 'lymph_node_status'} & data.keys()


def test_numeric_pdl1_zero_and_coded_hrd_and_histology():
    person = PersonFactory()
    for code in ('29593-1', '105304-0', '105305-7', '105303-2'):
        MeasurementFactory(person=person, measurement_source_value=code, value_as_number=0)
    for code, value in [('107286-7', 'Negative'), ('59847-4', 'Ductal carcinoma')]:
        MeasurementFactory(person=person, measurement_source_value=code, value_as_concept=ConceptFactory(concept_name=value))
    data = _get_biomarker_data(person)
    for field in ('ki67_proliferation_index', 'pd_l1_tumor_cells', 'pd_l1_ic_percentage', 'pd_l1_combined_positive_score'):
        assert data[field] == 0
    assert data['hrd_status'] == 'Negative'
    assert data['histologic_type'] == 'Ductal carcinoma'


@pytest.mark.parametrize('value,axis,expected', [('M0', 'M', False), ('cM1', 'M', True), ('pM1', 'M', True), ('MX', 'M', None), ('Unknown', 'M', None), ('N0', 'N', False), ('ypN1mi', 'N', True), ('NX', 'N', None)])
def test_stage_presence_is_three_valued(value, axis, expected):
    assert stage_presence(value, axis) is expected


def test_pathological_staging_uses_latest_linked_assessment_without_cross_tumor_mix():
    person = PersonFactory()
    link = ConceptFactory()
    MeasurementFactory(person=person, measurement_source_value='21905-5', value_as_string='T4',
        measurement_date=date(2024, 1, 1), measurement_event_id=1, meas_event_field_concept=link)
    MeasurementFactory(person=person, measurement_source_value='21899-0', value_as_string='T1',
        measurement_date=date(2024, 2, 1), measurement_event_id=2, meas_event_field_concept=link, qualifier_source_value='yp')
    MeasurementFactory(person=person, measurement_source_value='21900-6', value_as_string='N0',
        measurement_date=date(2024, 2, 1), measurement_event_id=2, meas_event_field_concept=link, qualifier_source_value='yp')
    data = _get_staging_data(person)
    assert data['tumor_stage'] == 'T1'
    assert data['nodes_stage'] == 'N0'
    assert data['staging_modalities'] == 'yp'
    assert data['lymph_node_status'] == 'Negative'


def test_unknown_receptor_is_not_tnbc_negative_and_menopause_is_exact():
    person = PersonFactory()
    for code, value in [('16112-5', 'Negative'), ('16113-3', 'Negative'), ('48676-1', 'Unknown')]:
        MeasurementFactory(person=person, measurement_source_value=code, value_as_string=value)
    ObservationFactory(person=person, observation_concept=ConceptFactory(concept_name='Age at menopause'), value_as_number=50)
    record = refresh_patient_record(person)
    assert record.tnbc_status is None
    assert record.menopausal_status is None
