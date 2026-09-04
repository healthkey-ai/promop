from datetime import date, timedelta

import pytest

from omop_core.services.patient_record_service import (
    _get_biomarker_data,
    _get_genetic_mutations,
    _get_genomics_pathology_data,
    _get_mm_specific_data,
    _get_treatment_data,
    _get_wearable_data,
    refresh_patient_record,
)
from tests.factories import (
    ConceptFactory,
    DrugExposureFactory,
    MeasurementFactory,
    ObservationFactory,
    PatientRecordFactory,
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


def test_genomics_pathology_fields_use_dated_loinc_measurements():
    person = PersonFactory()
    result = ConceptFactory(concept_name='Positive')

    # The newest code-specific result wins.  These fixtures intentionally use
    # source values so the contract does not depend on an installed Athena DB.
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 1),
        measurement_source_value='85337-4', value_as_string='NGS', value_as_number=17,
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 2),
        measurement_source_value='31208-2', value_as_string='Primary biopsy',
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 3),
        measurement_source_value='69548-6', value_as_string='Indeterminate',
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 4),
        measurement_source_value='82185-1', value_as_concept=result,
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 5),
        measurement_source_value='92837-4', value_as_concept=result,
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 6),
        measurement_source_value='21907-1', value_as_string='Negative',
    )
    MeasurementFactory(
        person=person, measurement_date=date(2024, 5, 7),
        measurement_source_value='44648-4', value_as_number=2,
    )

    data = _get_genomics_pathology_data(person)

    assert data == {
        'test_methodology': 'NGS',
        'oncotype_dx_score': 17,
        'test_date': date(2024, 5, 3),
        'test_specimen_type': 'Primary biopsy',
        'report_interpretation': 'Indeterminate',
        'androgen_receptor_status': 'Positive',
        'lymph_node_status': 'Positive',
        'metastasis_status': 'Negative',
        'biopsy_grade_depr': '2',
    }


def test_mrd_requires_a_vocabulary_backed_concept():
    person = PersonFactory()
    mrd = ConceptFactory(
        vocabulary__vocabulary_id='NCIt',
        concept_name='Minimal residual disease status',
    )
    ObservationFactory(
        person=person, observation_date=date(2024, 5, 1),
        observation_concept=mrd, value_as_string='Negative',
    )
    # A similarly named LOCAL concept must not become a clinical mapping.
    local_mrd = ConceptFactory(
        vocabulary__vocabulary_id='LOCAL',
        concept_name='Minimal residual disease status',
    )
    ObservationFactory(
        person=person, observation_date=date(2025, 5, 1),
        observation_concept=local_mrd, value_as_string='Positive',
    )

    assert _get_genomics_pathology_data(person)['mrd_status'] == 'Negative'


def test_refresh_clears_removed_genomics_pathology_facts():
    person = PersonFactory()
    record = PatientRecordFactory(
        person=person,
        test_methodology='legacy patch',
        oncotype_dx_score=99,
        mrd_status='Positive',
    )
    measurement = MeasurementFactory(
        person=person, measurement_source_value='85337-4',
        value_as_string='IHC', value_as_number=12,
    )

    refreshed = refresh_patient_record(person)
    assert refreshed.test_methodology == 'IHC'
    assert refreshed.oncotype_dx_score == 12

    measurement.delete()
    refreshed = refresh_patient_record(person)
    record.refresh_from_db()
    assert refreshed.pk == record.pk
    assert record.test_methodology is None
    assert record.oncotype_dx_score is None
    assert record.mrd_status is None


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


def test_treatment_without_persisted_episode_does_not_project_a_line():
    person = PersonFactory()

    # Drug exposure import precedes ARTEMIS episode generation.  Refresh must
    # not run an in-memory ARTEMIS-lite grouping: only persisted Episode +
    # EpisodeEvent records can populate the treatment-line projection.
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='docetaxel 20 MG/ML Injection [DOCETAXEL EG]'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='cyclophosphamide 500 MG Injection'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )

    data = _get_treatment_data(person)

    assert 'first_line_therapy' not in data
    assert 'first_line_therapy_id' not in data
    assert 'therapy_lines_count' not in data


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
