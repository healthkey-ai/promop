from datetime import date, timedelta

import pytest

from omop_core.services.patient_record_service import (
    _get_biomarker_data,
    _get_genetic_mutations,
    _get_mm_specific_data,
    _get_treatment_data,
    _get_wearable_data,
)
from tests.factories import (
    ConceptFactory,
    DrugExposureFactory,
    MeasurementFactory,
    ObservationFactory,
    PersonFactory,
)

pytestmark = pytest.mark.django_db


def test_histologic_type_uses_measurement_source_value_fallback():
    person = PersonFactory()

    MeasurementFactory(
        person=person,
        measurement_source_value='59847-4',
        value_as_string='Invasive ductal carcinoma of breast',
    )

    data = _get_biomarker_data(person)

    assert data['histologic_type'] == 'Invasive ductal carcinoma of breast'


def test_genetic_mutations_use_measurement_source_value_fallback():
    person = PersonFactory()

    MeasurementFactory(
        person=person,
        measurement_date=date(2024, 5, 1),
        measurement_source_value='21636-6',
        value_as_string='BRCA1 pathogenic variant',
    )

    data = _get_genetic_mutations(person)

    assert data['genetic_mutations'] == [
        {
            'gene': 'brca1',
            'variant': 'BRCA1 pathogenic variant',
            'test_date': '2024-05-01',
        },
    ]


def test_wearable_metrics_use_measurement_source_value_fallbacks():
    person = PersonFactory()
    anchor = date(2024, 1, 30)

    for day_offset in range(7):
        sample_date = anchor - timedelta(days=day_offset)
        MeasurementFactory(
            person=person,
            measurement_date=sample_date,
            measurement_source_value='55423-8',
            value_as_number=7000 + day_offset * 100,
        )
        MeasurementFactory(
            person=person,
            measurement_date=sample_date,
            measurement_source_value='93832-4',
            value_as_number=7.0 + day_offset * 0.1,
        )

    data = _get_wearable_data(person)

    assert data['wearable_coverage_ratio_30d'] == pytest.approx(0.23)
    assert data['median_daily_steps_30d'] == 7300
    assert data['sleep_duration_hours_avg_30d'] == pytest.approx(7.3)


def test_treatment_fallback_derives_bc_regimen_concept_from_same_day_combo():
    person = PersonFactory()

    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='docetaxel 20 MG/ML Injection [DOCETAXEL EG]'),
        drug_exposure_start_date=date(2024, 1, 1),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='cyclophosphamide 500 MG Injection'),
        drug_exposure_start_date=date(2024, 1, 1),
    )

    data = _get_treatment_data(person)

    assert data['first_line_therapy'] == 'Cyclophosphamide and Docetaxel (TC)'
    assert data['first_line_therapy_id'] == 35804232
    assert data['therapy_lines_count'] == 1


def test_treatment_fallback_collapses_staggered_bc_backfill_into_one_line():
    person = PersonFactory()

    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='doxorubicin hydrochloride 2 MG/ML Injection'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='cyclophosphamide 500 MG Injection'),
        drug_exposure_start_date=date(2024, 1, 22),
        drug_exposure_end_date=date(2024, 2, 11),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='Paclitaxel 6 MG/ML Injection [Aj-Paclitaxel]'),
        drug_exposure_start_date=date(2024, 2, 12),
        drug_exposure_end_date=date(2024, 3, 4),
    )

    data = _get_treatment_data(person)

    assert data['first_line_therapy'] == 'AC-T'
    assert data['first_line_therapy_id'] == 35101507
    assert data['therapy_lines_count'] == 1


def test_treatment_fallback_normalizes_thp_product_names_to_regimen():
    person = PersonFactory()

    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='Paclitaxel 6 MG/ML Injection [Aj-Paclitaxel]'),
        drug_exposure_start_date=date(2024, 1, 1),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='trastuzumab 150 MG Injectable Solution'),
        drug_exposure_start_date=date(2024, 1, 22),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='pertuzumab; parenteral'),
        drug_exposure_start_date=date(2024, 2, 12),
    )

    data = _get_treatment_data(person)

    assert data['first_line_therapy'] == 'THP'
    assert data['first_line_therapy_id'] == 1525210
    assert data['therapy_lines_count'] == 1


def test_mm_specific_data_merges_measurement_and_observation_sources():
    person = PersonFactory()

    MeasurementFactory(
        person=person,
        measurement_source_value='24646-7',
        value_as_number=1,
    )
    ObservationFactory(
        person=person,
        observation_source_value='47082-2',
        value_as_number=1,
    )

    data = _get_mm_specific_data(person)

    assert data['bone_lesions'] == 'Present'
    assert data['plasma_cell_leukemia'] is True
