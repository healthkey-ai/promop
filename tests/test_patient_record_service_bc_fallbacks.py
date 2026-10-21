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


def test_legacy_lab_alias_fields_are_populated_from_their_canonical_code():
    """The widget still binds the pre-#471 names for three chemistry labs.

    The canonical LOINC-backed columns were the only ones derivation wrote, so the
    widget's `serum_creatinine_level` / `serum_calcium_level` / `albumin_level`
    boxes were permanently blank. The alias map is the backwards-compatibility
    seam; assert the values actually reach those columns, not just that the map
    has keys.
    """
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('2160-0', 1.1),   # creatinine, canonical serum_creatinine_mg_dl
        ('17861-6', 9.4),  # calcium, canonical serum_calcium_mg_dl
        ('1751-7', 3.9),   # albumin, canonical albumin_g_dl
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_number=value,
        )

    record = refresh_patient_record(person)

    assert float(record.serum_creatinine_level) == 1.1
    assert float(record.serum_calcium_level) == 9.4
    assert float(record.albumin_level) == 3.9


def test_newer_legacy_named_lab_beats_an_older_canonical_alias():
    """Alias projection must not copy an older canonical result over a newer fact.

    A legacy feed can supply the same analyte under its display name after the
    canonical LOINC row. The newest result wins, and its unit travels with it.
    """
    person = PersonFactory()
    PatientRecordFactory(person=person)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='2160-0'),
        measurement_source_value='2160-0',
        measurement_date=date(2025, 1, 1),
        value_as_number=1.1,
        unit_source_value='mg/dL',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(
            concept_code='LEGACY-CREATININE', concept_name='Creatinine',
        ),
        measurement_source_value='Creatinine',
        measurement_date=date(2025, 2, 1),
        value_as_number=2.4,
        unit_source_value='mg/dL',
    )

    record = refresh_patient_record(person)

    assert float(record.serum_creatinine_mg_dl) == 2.4
    assert float(record.serum_creatinine_level) == 2.4
    assert record.serum_creatinine_level_units == 'mg/dL'


def test_legacy_chemistry_si_units_are_converted_before_renal_adequacy():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for concept_name, value, unit in (
        ('Creatinine', 100, 'umol/L'),
        ('Calcium', 2.5, 'mmol/L'),
        ('Albumin', 40, 'g/L'),
    ):
        MeasurementFactory(
            person=person,
            measurement_concept=ConceptFactory(
                concept_code=f'LEGACY-{concept_name.upper()}',
                concept_name=concept_name,
            ),
            measurement_source_value=concept_name,
            measurement_date=date(2026, 1, 15),
            value_as_number=value,
            unit_source_value=unit,
        )

    record = refresh_patient_record(person)

    assert float(record.serum_creatinine_mg_dl) == pytest.approx(1.131, abs=0.001)
    assert record.serum_creatinine_level_units == 'mg/dL'
    assert float(record.serum_calcium_mg_dl) == pytest.approx(10.02, abs=0.01)
    assert float(record.albumin_g_dl) == pytest.approx(4.0, abs=0.01)
    assert record.renal_adequacy_status is True


def test_unsupported_chemistry_unit_does_not_fall_back_to_an_older_result():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(concept_code='2160-0'),
        measurement_source_value='2160-0',
        measurement_date=date(2025, 1, 1),
        value_as_number=1.1,
        unit_source_value='mg/dL',
    )
    MeasurementFactory(
        person=person,
        measurement_concept=ConceptFactory(
            concept_code='LEGACY-CREATININE', concept_name='Creatinine',
        ),
        measurement_source_value='Creatinine',
        measurement_date=date(2025, 2, 1),
        value_as_number=100,
        unit_source_value='banana',
    )

    record = refresh_patient_record(person)

    assert record.serum_creatinine_mg_dl is None
    assert record.serum_creatinine_level is None


def test_hr_status_recognizes_plus_minus_receptor_results():
    """A saved ER+/PR+ result must still derive HR+ after the write round-trip.

    The write path stores the widget value (`ER+`), not the word `Positive`.
    `_compute_derived_fields` only looked for "positive", so HR status was cleared
    on the next refresh and the demo showed a blank/incorrect field.
    """
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+'),
        ('16113-3', 'PR+'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.estrogen_receptor_status == 'ER+'
    assert record.progesterone_receptor_status == 'PR+'
    assert record.her2_status == 'HER2-'
    assert record.hr_status == 'HR+'
    assert record.tnbc_status is False


def test_tnbc_status_recognizes_minus_receptor_results():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER-'),
        ('16113-3', 'PR-'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.hr_status == 'HR-'
    assert record.tnbc_status is True


def test_hr_and_tnbc_recognize_expression_qualified_positive_receptors():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+ with low expression'),
        ('16113-3', 'PR-'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.estrogen_receptor_status == 'ER+ WITH LOW EXPRESSION'
    assert record.hr_status == 'HR+'
    assert record.tnbc_status is False


def test_definite_positive_receptor_excludes_tnbc_when_another_result_is_unknown():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+'),
        ('16113-3', 'Unknown'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.hr_status == 'HR+'
    assert record.tnbc_status is False


def test_assay_score_is_not_treated_as_a_positive_receptor():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER-'),
        ('16113-3', 'PR-'),
        ('48676-1', 'IHC 2+'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.tnbc_status is None


def test_mixed_symbol_and_word_receptor_result_stays_unknown():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+ / Negative'),
        ('16113-3', 'PR-'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.estrogen_receptor_status == 'ER+ / NEGATIVE'
    assert record.hr_status is None
    assert record.tnbc_status is None


def test_hyphenated_word_receptor_results_are_not_mixed_signs():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER-positive'),
        ('16113-3', 'PR-negative'),
        ('48676-1', 'HER2-negative'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.hr_status == 'HR+'
    assert record.tnbc_status is False


def test_mixed_symbol_and_word_without_slash_stays_unknown():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+; Negative'),
        ('16113-3', 'PR-'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.estrogen_receptor_status == 'ER+; NEGATIVE'
    assert record.hr_status is None
    assert record.tnbc_status is None


def test_positive_receptor_excludes_tnbc_when_another_receptor_is_absent():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.hr_status == 'HR+'
    assert record.tnbc_status is False


def test_mixed_receptor_result_is_preserved_and_stays_unknown():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'Negative/Equivocal'),
        ('16113-3', 'PR-'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.estrogen_receptor_status == 'NEGATIVE/EQUIVOCAL'
    assert record.hr_status is None
    assert record.tnbc_status is None


def test_ambiguous_receptor_sign_does_not_derive_hr_or_tnbc():
    person = PersonFactory()
    PatientRecordFactory(person=person)
    for code, value in (
        ('16112-5', 'ER+/-'),
        ('16113-3', 'PR-'),
        ('48676-1', 'HER2-'),
    ):
        MeasurementFactory(
            person=person,
            measurement_source_value=code,
            measurement_date=date(2026, 1, 15),
            value_as_string=value,
        )

    record = refresh_patient_record(person)

    assert record.hr_status is None
    assert record.tnbc_status is None


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


def test_treatment_fallback_derives_bc_regimen_concept_from_same_day_combo():
    person = PersonFactory()

    # The TC regimen concept exists on real DBs (HemOnc-loaded); seed it so
    # derivation can surface its full concept name, not just the 'TC' abbreviation.
    ConceptFactory(concept_id=35804232, concept_name='Cyclophosphamide and Docetaxel (TC)')

    # Same-day combination — explicit end dates so LOT inference groups the two
    # exposures into one line (DrugExposureFactory's default end date is
    # unrelated to these start dates).
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

    # Contiguous end dates so LOT inference groups the three product-named
    # exposures into a single line (DrugExposureFactory's default end date is
    # unrelated to these start dates).
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='Paclitaxel 6 MG/ML Injection [Aj-Paclitaxel]'),
        drug_exposure_start_date=date(2024, 1, 1),
        drug_exposure_end_date=date(2024, 1, 21),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='trastuzumab 150 MG Injectable Solution'),
        drug_exposure_start_date=date(2024, 1, 22),
        drug_exposure_end_date=date(2024, 2, 11),
    )
    DrugExposureFactory(
        person=person,
        drug_concept=ConceptFactory(concept_name='pertuzumab; parenteral'),
        drug_exposure_start_date=date(2024, 2, 12),
        drug_exposure_end_date=date(2024, 3, 4),
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
