from datetime import date

import pytest

from omop_core.services.breast_cancer import stage_presence
from omop_core.services.patient_record_service import _get_biomarker_data, _get_genomics_pathology_data, _get_staging_data, refresh_patient_record
from tests.factories import ConceptFactory, MeasurementFactory, ObservationFactory, PersonFactory

pytestmark = pytest.mark.django_db


def test_indexed_biomarkers_select_newer_source_fact_across_code_buckets():
    from datetime import date
    from omop_core.services.patient_record_service import _get_biomarker_data
    person = PersonFactory()
    question = ConceptFactory(concept_code='29593-1')
    MeasurementFactory(person=person, measurement_concept=question, value_as_number=10,
                       measurement_date=date(2024, 1, 1))
    MeasurementFactory(person=person, measurement_source_value='29593-1', value_as_number=30,
                       measurement_date=date(2024, 2, 1))
    assert _get_biomarker_data(person)['ki67_proliferation_index'] == 30


def test_indexed_oncotype_keeps_naaccr_namespace_and_latest_source():
    from datetime import date
    from omop_core.services.patient_record_service import _get_genomics_pathology_data
    from tests.factories import VocabularyFactory
    person = PersonFactory()
    unrelated = ConceptFactory(concept_code='3903', vocabulary=VocabularyFactory(vocabulary_id='Unrelated'))
    MeasurementFactory(person=person, measurement_concept=unrelated, value_as_number=99,
                       measurement_date=date(2024, 3, 1))
    MeasurementFactory(person=person, measurement_source_value='3903', value_as_number=18,
                       measurement_date=date(2024, 2, 1))
    assert _get_genomics_pathology_data(person)['oncotype_dx_score'] == 18


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


def test_newer_unlinked_observation_outranks_older_linked_measurement():
    person = PersonFactory()
    MeasurementFactory(person=person, measurement_source_value='21905-5', value_as_string='T4',
        measurement_date=date(2024, 1, 1), measurement_event_id=1,
        meas_event_field_concept=ConceptFactory())
    ObservationFactory(person=person, observation_source_value='21905-5', value_as_string='T1',
        observation_date=date(2024, 2, 1))
    ObservationFactory(person=person, observation_source_value='21908-9', value_as_string='I',
        observation_date=date(2024, 2, 1))
    ObservationFactory(person=person, observation_source_value='21908-9-riss', value_as_string='III',
        observation_date=date(2024, 1, 1))
    data = _get_staging_data(person)
    assert data['tumor_stage'] == 'T1'
    assert data['stage'] == 'I'


def test_histology_uses_latest_date_across_measurement_and_observation():
    from omop_core.models import FieldConceptMapping
    person = PersonFactory()
    question = ConceptFactory(vocabulary__vocabulary_id='LOINC', concept_code='59847-4',
                              concept_name='Histology and Behavior ICD-O-3 Cancer')
    FieldConceptMapping.objects.update_or_create(field_name='histologic_type', defaults={
        'concept': question, 'vocabulary_id': 'LOINC', 'concept_code': '59847-4',
        'source_value': '59847-4', 'status': 'approved', 'omop_table': 'measurement', 'value_kind': 'string'})
    MeasurementFactory(person=person, measurement_concept=question, measurement_source_value='59847-4', value_as_string='Older histology',
        measurement_date=date(2024, 1, 1))
    ObservationFactory(person=person, observation_source_value='59847-4', value_as_string='Current histology',
        observation_date=date(2024, 2, 1))
    assert _get_biomarker_data(person)['histologic_type'] == 'Current histology'
    assert refresh_patient_record(person).histologic_type == 'Current histology'
