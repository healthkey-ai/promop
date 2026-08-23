"""
patient_record_service.py — Reusable service for deriving PatientRecord from OMOP tables.

Usage:
    from omop_core.services.patient_record_service import refresh_patient_record
    patient_info = refresh_patient_record(person)
"""

import dataclasses
import logging
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from django.db import models
from django.db.models import DateTimeField, Q
from django.db.models.functions import Cast, Coalesce
from django.db import transaction
from django.utils import timezone
from omop_core.models import (
    Person, PatientRecord, ConditionOccurrence, Concept,
    Measurement, Observation, DrugExposure, Location, ProcedureOccurrence,
    Death, ConceptRelationship, PERSON_YEAR_PLACEHOLDERS,
)
from omop_core.services.concept_cache import concept_by_id as _cc_by_id, concept_by_loinc as _cc_by_loinc
from omop_core.services.mappings import (
    WEARABLE_CONCEPT_CODE, WEARABLE_ARTIFACT_BOUNDS, WEARABLE_MIN_VALID_DAYS,
    WEARABLE_TREND_IMPROVING_PCT, WEARABLE_TREND_DECLINING_PCT,
)
from omop_core.services.clinical_units import (
    canonical_wbc_unit, flc_to_canonical, wbc_to_canonical,
)
from omop_core.services.lot_regimens import (
    get_regimen_concept_id,
    get_regimen_name,
    get_regimen_concept_id_by_name,
    normalize_drug_name,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# Concept 0 is seeded with this name; it must never leak into user-visible
# labels.  _get_disease_data already guards against it; the same guard must be
# applied everywhere a concept_name is surfaced (therapy labels, concomitant
# medications, etc.).  See #458.
_SENTINEL_CONCEPT_NAME = 'No matching concept'


def _usable_concept_name(concept) -> str | None:
    """Return the concept's name unless it is the unmapped sentinel (concept 0)."""
    if concept is None:
        return None
    name = concept.concept_name
    if not name or name == _SENTINEL_CONCEPT_NAME:
        return None
    return name

# Bump this whenever aggregation or computation logic changes in any section
# extractor or in _compute_derived_fields.  See DERIVATION_CHANGELOG.md.
DERIVATION_VERSION = 5

# Fields that are entirely derived from OMOP tables and must be reset before
# each refresh so deletions are reflected (not just additions).
_OMOP_DERIVED_FIELDS = [
    # Demographics. patient_age is a pure function of the birth date, so a value
    # left over from an earlier derivation is stale by construction and has to be
    # cleared. date_of_birth is deliberately not cleared: a manually supplied
    # DOB remains useful when Person has only the registration placeholder.
    'patient_age',
    # Person extension fields projected onto PatientRecord for compatibility.
    'email', 'phone_number', 'facility_name', 'validated', 'validated_by',
    'validation_date', 'suppress_demographics_for_others',
    # Disease / condition
    'disease', 'diagnosis_date', 'death_date', 'condition_clinical_status', 'disease_slug',
    # Therapy lines
    'first_line_therapy', 'first_line_date', 'first_line_start_date', 'first_line_end_date',
    'first_line_therapy_id',
    'second_line_therapy', 'second_line_date', 'second_line_start_date', 'second_line_end_date',
    'second_line_therapy_id',
    'later_therapy', 'later_date', 'later_start_date', 'later_end_date',
    'later_therapies', 'later_therapy_ids',
    'first_line_component_ids', 'second_line_component_ids',
    'later_component_ids', 'therapy_component_ids',
    'first_line_therapy_type_ids', 'second_line_therapy_type_ids',
    'later_therapy_type_ids', 'therapy_type_ids',
    'therapy_ids_provenance',
    'therapy_lines_count', 'last_treatment',
    'concomitant_medications',
    # Treatment assertions and summaries. These are set from dated OMOP rows
    # below; clearing them prevents legacy PatientRecord patches surviving a
    # source deletion.
    'first_line_outcome', 'first_line_intent', 'first_line_discontinuation_reason',
    'second_line_outcome', 'second_line_intent', 'second_line_discontinuation_reason',
    'later_outcome', 'later_intent', 'later_discontinuation_reason',
    'therapy_intent', 'reason_for_discontinuation', 'relapse_count',
    'treatment_refractory_status', 'washout_period_duration',
    'remission_duration_min', 'line_of_therapy', 'prior_therapy',
    # Legacy labs (derived via name-based Measurement lookup)
    'hemoglobin_level', 'hemoglobin_level_units',
    'white_blood_cell_count', 'white_blood_cell_count_units',
    'serum_creatinine_level', 'serum_creatinine_level_units',
    'platelet_count', 'platelet_count_units',
    'serum_calcium_level', 'serum_calcium_level_units',
    'serum_bilirubin_level_total', 'serum_bilirubin_level_total_units',
    'albumin_level', 'albumin_level_units',
    # CBC (LOINC-derived)
    'hemoglobin_g_dl', 'hematocrit_percent', 'wbc_count_thousand_per_ul',
    'rbc_million_per_ul', 'platelet_count_thousand_per_ul',
    'anc_thousand_per_ul', 'alc_thousand_per_ul', 'amc_thousand_per_ul',
    # CMP (LOINC-derived)
    'serum_calcium_mg_dl', 'serum_creatinine_mg_dl', 'creatinine_clearance_ml_min',
    'egfr_ml_min_173m2', 'bun_mg_dl', 'sodium_meq_l', 'potassium_meq_l', 'magnesium_mg_dl',
    # LFT / cardiac (LOINC-derived)
    'bilirubin_total_mg_dl', 'serum_bilirubin_level_direct', 'alt_u_l', 'ast_u_l', 'alkaline_phosphatase_u_l',
    'albumin_g_dl', 'total_protein', 'troponin_ng_ml', 'bnp_pg_ml',
    'glucose_mg_dl', 'hba1c_percent', 'ldh_u_l',
    # These FHIR Observations are persisted as OMOP Measurements and must not
    # retain a projection-only copy after their source is removed.
    'inr', 'pt_seconds', 'ptt_seconds', 'cea_ng_ml', 'ca19_9_u_ml',
    'psa_ng_ml', 'phosphorus',
    # Remaining numeric Measurement projections (#504).
    'heartrate_variability', 'ejection_fraction', 'qtcf_value',
    'liver_enzyme_levels_ast', 'liver_enzyme_levels_alt',
    'liver_enzyme_levels_alp', 'liver_enzyme_levels',
    # Legacy aliases for deduplicated LOINC fields (issue #471).
    # These model columns still exist for backward compatibility; they are
    # populated with the same value as their canonical counterpart during
    # derivation. See _LAB_FIELD_ALIASES below.
    'calcium_mg_dl', 'creatinine_mg_dl', 'egfr', 'blood_urea_nitrogen',
    'serum_sodium', 'serum_potassium', 'magnesium', 'alkaline_phosphatase',
    'ldh_level', 'ldh',
    'absolute_neutrophile_count', 'red_blood_cell_count',
    'estimated_glomerular_filtration_rate', 'creatinine_clearance_rate',
    'serum_bilirubin_level', 'lactate_dehydrogenase_level',
    # Other markers (LOINC-derived)
    'beta2_microglobulin', 'c_reactive_protein', 'esr',
    # MM disease burden (LOINC-derived)
    'monoclonal_protein_serum', 'monoclonal_protein_urine',
    'kappa_flc', 'lambda_flc', 'clonal_plasma_cells', 'free_light_chain_ratio',
    # MM boolean/coded fields (derived by _get_mm_specific_data)
    'plasma_cell_leukemia', 'bone_lesions', 'meets_crab', 'meets_slim',
    # MM cytogenetics + SCT (derived by _get_sct_cytogenetic_data)
    'cytogenic_markers', 'sct_date', 'stem_cell_transplant_history', 'sct_eligibility',
    # Vitals
    'systolic_blood_pressure', 'diastolic_blood_pressure', 'heartrate',
    'weight', 'weight_units', 'height', 'height_units', 'temperature',
    # Performance
    'ecog_performance_status', 'ecog_assessment_date', 'karnofsky_performance_score',
    # Biomarkers
    'pd_l1_tumor_cells', 'pd_l1_assay',
    'pd_l1_ic_percentage', 'pd_l1_combined_positive_score', 'ki67_proliferation_index',
    'estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status', 'tnbc_status',
    'hr_status', 'hrd_status', 'menopausal_status',
    # Genomics and pathology report metadata.  These are all projections of
    # dated OMOP Measurement/Observation facts; keeping them here is important
    # so deleting a source report cannot leave a stale clinical assertion.
    'molecular_markers', 'test_methodology', 'test_date', 'test_specimen_type',
    'report_interpretation', 'oncotype_dx_score', 'androgen_receptor_status',
    'lymph_node_status', 'metastasis_status', 'mrd_status',
    'biopsy_grade_depr',
    # Behavior / lifestyle
    'smoking_status', 'pack_years', 'alcohol_use', 'drinks_per_week',
    'exercise_frequency', 'exercise_minutes_per_week', 'diet_type',
    'sleep_hours_per_night', 'sleep_quality', 'stress_level', 'social_support',
    'employment_status', 'education_level', 'marital_status', 'insurance_type',
    'number_of_dependents', 'annual_household_income',
    # Dated clinical eligibility / social assertions (#505).  These columns
    # are nullable because no OMOP assertion means unknown, not the model's
    # historical default answer.
    'pregnancy_test_date', 'pregnancy_test_result_value',
    'contraceptive_use', 'consent_capability', 'caregiver_availability_status',
    'no_mental_health_disorder_status', 'no_substance_use_status',
    'no_geographic_exposure_risk',
    # Staging
    'stage', 'tumor_stage', 'nodes_stage', 'distant_metastasis_stage', 'staging_modalities',
    'metastatic_status', 'bone_only_metastasis_status',
    # Tumour measurements.  LOINC 21889-1 is tumour size; a lymph-node
    # measurement using that code is accepted only with an explicit OMOP
    # qualifier_source_value of ``lymph-node``.
    'tumor_size', 'largest_lymph_node_size',
    # Biopsy
    'histologic_type', 'biopsy_grade',
    # Genomics
    'genetic_mutations',
    # CLL (absolute_lymphocyte_count is no longer actively derived —
    # canonical source is alc_thousand_per_ul; kept here so the API
    # contract test still treats it as a read-only derived column)
    'absolute_lymphocyte_count',
    'serum_beta2_microglobulin_level',
    'binet_stage', 'tumor_burden', 'disease_activity',
    'bone_marrow_involvement', 'hepatomegaly', 'splenomegaly', 'lymphadenopathy',
    'btk_inhibitor_refractory', 'bcl2_inhibitor_refractory', 'lymphocyte_doubling_time',
    # Lymphoma
    'flipi_score', 'gelf_criteria_status', 'tumor_grade',
    'transformed_to_dlbcl', 'dlbcl_transformation_date', 'post_transformation_outcome',
    # Assessment
    'measurable_disease_by_recist_status',
    # Clinical (breast cancer)
    'peripheral_neuropathy_grade', 'toxicity_grade', 'renal_adequacy_status',
    # Procedures
    'prior_procedures',
    # BMI (computed from weight + height)
    'bmi',
    # Wearable summaries
    'wearable_last_sync_at', 'wearable_coverage_ratio_30d',
    'median_daily_steps_30d', 'active_minutes_per_day_30d', 'activity_trend_30d',
    'resting_heart_rate_avg_30d', 'hrv_sdnn_avg_30d', 'hrv_rmssd_avg_30d',
    'oxygen_saturation_min_30d', 'oxygen_saturation_avg_30d', 'respiratory_rate_avg_30d',
    'sleep_duration_hours_avg_30d', 'vo2_max_avg_30d',
    'distance_km_per_day_30d', 'walking_speed_avg_30d', 'walking_step_length_avg_30d',
    'walking_double_support_pct_avg_30d', 'walking_hr_avg_30d',
    'flights_climbed_per_day_30d', 'active_energy_per_day_30d', 'basal_energy_per_day_30d',
    'body_mass_avg_30d',
]

# Public ownership contract for API serializers and writers.  This is broader
# than ``_OMOP_DERIVED_FIELDS`` because a few Person/Location fields are
# populated by refresh but intentionally not cleared when their source is
# incomplete.  They are still OMOP-backed facts, not projection-owned values.
PATIENT_RECORD_OMOP_MAPPED_FIELDS = frozenset(_OMOP_DERIVED_FIELDS) | frozenset({
    'date_of_birth', 'gender', 'race', 'ethnicity', 'languages_skills',
    'country', 'region', 'city', 'postal_code', 'latitude', 'longitude',
    # These are populated by section extractors but are not cleared before a
    # refresh (mostly because they carry structured / negative findings). They
    # nevertheless originate from OMOP facts and must not become writable
    # projection exceptions merely because they are absent from the reset list.
    'autoimmune_cytopenias_refractory_to_steroids',
    'clonal_b_lymphocyte_count', 'clonal_bone_marrow_b_lymphocytes',
    'concomitant_medication_details',
    'first_line_outcome', 'second_line_outcome', 'later_outcome',
    'flipi_score_options', 'hepatitis_b_status', 'hepatitis_c_status',
    'hiv_status', 'no_hepatitis_b_status', 'no_hepatitis_c_status',
    'no_hiv_status', 'no_pre_existing_conditions', 'no_tobacco_use_status',
    'measurable_disease_imwg', 'measurable_disease_iwcll',
    'protein_expressions', 'richter_transformation', 'tobacco_use_details',
    'tp53_disruption',
    # These are clinical projection columns whose OMOP derivation is still
    # pending. They are deliberately API read-only now: allowing a temporary
    # PatientRecord PATCH would create a second clinical write authority and
    # make later re-derivation ambiguous. See docs/omop_to_patientrecord.md.
    'no_other_active_malignancies', 'preexisting_conditions', 'myeloma_type',
    'progression', 'condition_code_icd_10', 'condition_code_snomed_ct',
    'supportive_therapies', 'supportive_therapy_date',
    'supportive_therapy_start_date', 'supportive_therapy_end_date',
    'supportive_therapy_intent', 'absolute_neutrophile_count_units',
    'red_blood_cell_count_units', 'serum_bilirubin_level_direct_units',
    'pulmonary_function_test_result', 'bone_imaging_result',
    'no_pregnancy_or_lactation_status', 'pregnancy_test_result',
    'no_concomitant_medication_status', 'substance_use_details',
    'geographic_exposure_risk_details', 'no_active_infection_status',
    'concomitant_medication_date', 'planned_therapies', 'spleen_size',
})


# LOINC code → (PatientRecord field name, coercion function)
# Used to derive UI lab fields from the OMOP Measurement table.
_LOINC_LAB_FIELDS = {
    # CBC
    '718-7':   ('hemoglobin_g_dl',               float),
    '20570-8': ('hematocrit_percent',             float),
    '6690-2':  ('wbc_count_thousand_per_ul',      float),
    '789-8':   ('rbc_million_per_ul',             float),
    '777-3':   ('platelet_count_thousand_per_ul', float),
    '751-8':   ('anc_thousand_per_ul',            float),
    '731-0':   ('alc_thousand_per_ul',            float),
    '742-7':   ('amc_thousand_per_ul',            float),
    # Hematocrit — service default is 20570-8 (calculated); mCODE uses 4544-3 (automated count)
    '4544-3':  ('hematocrit_percent',             float),
    # CMP
    '17861-6': ('serum_calcium_mg_dl',            float),
    '49765-1': ('serum_calcium_mg_dl',            float),  # mCODE: Calcium in Blood
    '2160-0':  ('serum_creatinine_mg_dl',         float),
    '38483-4': ('serum_creatinine_mg_dl',         float),  # mCODE: Creatinine in Blood
    '2164-2':  ('creatinine_clearance_ml_min',    float),
    '62238-1': ('egfr_ml_min_173m2',              float),
    '33914-3': ('egfr_ml_min_173m2',              float),  # mCODE: eGFR CKD-EPI
    '3094-0':  ('bun_mg_dl',                      float),
    '6299-2':  ('bun_mg_dl',                      float),  # mCODE: BUN in Blood
    '2951-2':  ('sodium_meq_l',                   float),
    '2947-0':  ('sodium_meq_l',                   float),  # mCODE: Sodium in Blood
    '2823-3':  ('potassium_meq_l',                float),
    '6298-4':  ('potassium_meq_l',                float),  # mCODE: Potassium in Blood
    '2601-3':  ('magnesium_mg_dl',                float),
    # Glucose — service default is 2345-7 (Serum); mCODE uses 2339-0 (Blood)
    '2345-7':  ('glucose_mg_dl',                  int),
    '2339-0':  ('glucose_mg_dl',                  int),    # mCODE: Glucose in Blood
    # LFT / cardiac
    '1975-2':  ('bilirubin_total_mg_dl',          float),
    '1968-7':  ('serum_bilirubin_level_direct',  float),
    '1742-6':  ('alt_u_l',                        int),
    '1920-8':  ('ast_u_l',                        int),
    '6768-6':  ('alkaline_phosphatase_u_l',       int),
    '1751-7':  ('albumin_g_dl',                   float),
    '2885-2':  ('total_protein',                  float),
    '10839-9': ('troponin_ng_ml',                 float),
    '42637-9': ('bnp_pg_ml',                      int),
    '4548-4':  ('hba1c_percent',                  float),
    '2532-0':  ('ldh_u_l',                        int),
    # Coagulation and tumour-marker results are Measurements too.  Use their
    # exact LOINC identities; never infer an analyte from a display name.
    '6301-6':  ('inr',                            float),
    '5902-2':  ('pt_seconds',                     float),
    '3173-2':  ('ptt_seconds',                    float),
    '2039-6':  ('cea_ng_ml',                      float),
    '25390-6': ('ca19_9_u_ml',                    float),
    '2857-1':  ('psa_ng_ml',                      float),
    '2777-1':  ('phosphorus',                     float),
    # Cardiac / electrophysiology.  HRV here is the current SDNN reading;
    # the separate hrv_sdnn_avg_30d remains the wearable-window aggregate.
    '80404-7': ('heartrate_variability',          int),
    '8806-2':  ('ejection_fraction',              int),
    '8632-1':  ('qtcf_value',                     float),
    # Other markers
    '1952-1':  ('beta2_microglobulin',            float),
    '1988-5':  ('c_reactive_protein',             float),
    '30341-2': ('esr',                            int),
    # MM disease burden labs. Real-world data codes these differently from the
    # demo concepts this app seeds, so both spellings are listed. '33944-8' and
    # '33945-5' are not LOINC at all and only resolve against seeded rows.
    #
    # CAUTION: '33944-8' (seeded kappa) and '33944-0' (real LOINC lambda) differ
    # by one character and are opposite analytes. Do not tidy them together.
    '51435-6': ('monoclonal_protein_serum',       float),  # M-protein band 1, SPEP
    '33358-3': ('monoclonal_protein_serum',       float),  # M-protein, SPEP (3046299)
    '32730-5': ('monoclonal_protein_urine',       float),  # Urine M-spike 24h
    '33944-8': ('kappa_flc',                      float),  # seeded demo concept only
    '36916-5': ('kappa_flc',                      float),  # Kappa FLC, Serum (3034860)
    '80515-0': ('kappa_flc',                      float),  # Kappa FLC by nephelometry
    '33945-5': ('lambda_flc',                     float),  # seeded demo concept only
    '33944-0': ('lambda_flc',                     float),  # Lambda FLC, Serum (3047169)
    '80516-8': ('lambda_flc',                     float),  # Lambda FLC by nephelometry
    '48378-4': ('free_light_chain_ratio',         float),  # Kappa/lambda ratio (3053209)
    '80517-6': ('free_light_chain_ratio',         float),  # ratio by nephelometry
    '104546-7': ('free_light_chain_ratio',        float),  # ratio, newer LOINC
    # '26098-4' is "XR Ankle - left Views" in LOINC, so it used to project ankle
    # radiographs into clonal_plasma_cells. '11118-7' is the real marrow code.
    '11118-7': ('clonal_plasma_cells',            float),  # Plasma cells/100 cells in marrow (3003879)
}

# Keyed by field name not by code, so a new LOINC spelling above feeds both the
# lab projection and the SLiM criteria.
_SLIM_FIELDS = frozenset({
    'clonal_plasma_cells', 'kappa_flc', 'lambda_flc', 'free_light_chain_ratio',
})

# Projected in mg/L when the source unit says which unit it is in.
_FLC_FIELDS = frozenset({'kappa_flc', 'lambda_flc'})

# Legacy field aliases for deduplicated LOINC codes (issue #471).
#
# Before this fix, nine LOINC codes were each mapped by two or three
# PatientRecord fields in LAB_FIELD_TO_LOINC, causing write collisions and
# stale projections. The duplicates were removed; only the canonical
# unit-suffixed field survives in LAB_FIELD_TO_LOINC. During derivation the
# canonical value is copied to the legacy aliases so API consumers that read
# the old field names continue to work.
#
# canonical field           → [legacy aliases]
#
# Two canonicals carry more than one legacy alias. They must stay in a single
# entry each: this literal previously listed 'egfr_ml_min_173m2' and
# 'alkaline_phosphatase_u_l' twice, and Python kept only the last, so 'egfr' and
# 'alkaline_phosphatase' were cleared on every refresh and never repopulated.
# test_no_canonical_is_listed_twice guards the shape.
_LAB_FIELD_ALIASES = {
    'serum_calcium_mg_dl':    ['calcium_mg_dl'],
    'serum_creatinine_mg_dl': ['creatinine_mg_dl'],
    'egfr_ml_min_173m2':      ['egfr', 'estimated_glomerular_filtration_rate'],
    'bun_mg_dl':              ['blood_urea_nitrogen'],
    'sodium_meq_l':           ['serum_sodium'],
    'potassium_meq_l':        ['serum_potassium'],
    'magnesium_mg_dl':        ['magnesium'],
    'alkaline_phosphatase_u_l': ['alkaline_phosphatase', 'liver_enzyme_levels_alp'],
    'ldh_u_l':                ['ldh_level', 'ldh', 'lactate_dehydrogenase_level'],
    'anc_thousand_per_ul':    ['absolute_neutrophile_count'],
    'rbc_million_per_ul':     ['red_blood_cell_count'],
    'creatinine_clearance_ml_min': ['creatinine_clearance_rate'],
    'serum_bilirubin_level_direct': ['serum_bilirubin_level'],
    'ast_u_l': ['liver_enzyme_levels_ast'],
    'alt_u_l': ['liver_enzyme_levels_alt'],
}

# A FHIR Condition.stage summary is an asserted clinical fact, but the FHIR
# element does not require a LOINC coding. Keep its source identity explicit
# in OMOP Observation rather than writing the derived PatientRecord.stage
# field directly. A coded staging Observation remains preferred when present.
FHIR_CONDITION_STAGE_SOURCE_VALUE = 'FHIR-condition-stage'

# Source-value fallback map for environments where LOINC Concepts aren't loaded.
# Key  = measurement_source_value (as stored by the FHIR upload pipeline or PATCH write-through)
# Value = PatientRecord field name
_SOURCE_VALUE_LAB_FIELDS = {
    'Hemoglobin [Mass/volume] in Blood':          'hemoglobin_g_dl',
    'Hematocrit [Volume Fraction] of Blood':      'hematocrit_percent',
    'Leukocytes [#/volume] in Blood':             'wbc_count_thousand_per_ul',
    'Erythrocytes [#/volume] in Blood':           'rbc_million_per_ul',
    'Platelets [#/volume] in Blood':              'platelet_count_thousand_per_ul',
    'Neutrophils [#/volume] in Blood':            'anc_thousand_per_ul',
    'Lymphocytes [#/volume] in Blood':            'alc_thousand_per_ul',
    'Monocytes [#/volume] in Blood':              'amc_thousand_per_ul',
    'Calcium [Mass/volume] in Serum or Plasma':   'serum_calcium_mg_dl',
    # Short aliases used by the FHIR bundle generator
    'Serum Calcium':                              'serum_calcium_mg_dl',
    'Calcium':                                    'serum_calcium_mg_dl',
    'Creatinine [Mass/volume] in Serum or Plasma': 'serum_creatinine_mg_dl',
    'Serum Creatinine':                           'serum_creatinine_mg_dl',
    'Creatinine':                                 'serum_creatinine_mg_dl',
    'Creatinine Clearance':                       'creatinine_clearance_ml_min',
    'GFR/BSA pred CKD-EPI ArA':                  'egfr_ml_min_173m2',
    'Urea nitrogen [Mass/volume] in Serum or Plasma': 'bun_mg_dl',
    'Blood Urea Nitrogen':                        'bun_mg_dl',
    'Sodium [Moles/volume] in Serum or Plasma':   'sodium_meq_l',
    'Sodium':                                     'sodium_meq_l',
    'Potassium [Moles/volume] in Serum or Plasma': 'potassium_meq_l',
    'Potassium':                                  'potassium_meq_l',
    'Magnesium [Mass/volume] in Serum or Plasma': 'magnesium_mg_dl',
    'Magnesium':                                  'magnesium_mg_dl',
    'Bilirubin.total [Mass/volume] in Serum or Plasma': 'bilirubin_total_mg_dl',
    'Total Bilirubin':                            'bilirubin_total_mg_dl',
    'Bilirubin.direct [Mass/volume] in Serum or Plasma': 'serum_bilirubin_level_direct',
    'Direct Bilirubin':                           'serum_bilirubin_level_direct',
    'Alanine aminotransferase [Enzymatic activity/volum': 'alt_u_l',
    'ALT':                                        'alt_u_l',
    'Aspartate aminotransferase [Enzymatic activity/vol': 'ast_u_l',
    'AST':                                        'ast_u_l',
    'Alkaline phosphatase [Enzymatic activity/volume] i': 'alkaline_phosphatase_u_l',
    'Alkaline Phosphatase':                       'alkaline_phosphatase_u_l',
    'Albumin [Mass/volume] in Serum or Plasma':    'albumin_g_dl',
    'Albumin':                                     'albumin_g_dl',
    'Protein [Mass/volume] in Serum or Plasma':    'total_protein',
    'Troponin I.cardiac [Mass/volume] in Serum or Plasm': 'troponin_ng_ml',
    'BNP [Mass/volume] in Serum or Plasma':        'bnp_pg_ml',
    'Glucose [Mass/volume] in Serum or Plasma':    'glucose_mg_dl',
    'Glucose':                                     'glucose_mg_dl',
    'Hemoglobin A1c/Hemoglobin.total in Blood':    'hba1c_percent',
    'HbA1c':                                       'hba1c_percent',
    'Lactate dehydrogenase [Enzymatic activity/volume] ': 'ldh_u_l',
    'LDH':                                         'ldh_u_l',
    'Beta-2-Microglobulin [Mass/volume] in Serum or Pla': 'beta2_microglobulin',
    'C reactive protein [Mass/volume] in Serum or Plasm': 'c_reactive_protein',
    'Erythrocyte sedimentation rate':              'esr',
    # Short aliases used by the FHIR bundle generator
    'White blood cell count':                     'wbc_count_thousand_per_ul',
    'Red blood cell count':                       'rbc_million_per_ul',
    'Platelets':                                  'platelet_count_thousand_per_ul',
    'Absolute Neutrophil Count':                  'anc_thousand_per_ul',
    'Absolute Lymphocyte Count':                  'alc_thousand_per_ul',
    'Absolute Monocyte Count':                    'amc_thousand_per_ul',
    # MM-specific aliases (match display strings used by _mm_generator.py)
    'M-protein serum spike':                      'monoclonal_protein_serum',
    'M-protein urine 24h':                        'monoclonal_protein_urine',
    'Kappa free light chains':                    'kappa_flc',
    'Lambda free light chains':                   'lambda_flc',
    'Clonal plasma cells in bone marrow (%)':     'clonal_plasma_cells',
    'Free light chain ratio':                     'free_light_chain_ratio',
}

# Bounds the SLiM query. Without it the criteria walk every Measurement a person
# owns, which on a bulk loaded patient is tens of thousands of rows.
_SLIM_MATCH_VALUES = frozenset(
    [code for code, (field, _) in _LOINC_LAB_FIELDS.items() if field in _SLIM_FIELDS]
    + [name for name, field in _SOURCE_VALUE_LAB_FIELDS.items() if field in _SLIM_FIELDS]
)

# Historic, non-LOINC test names for the old paired lab fields. This fallback
# must be an explicit allowlist: substring matching turns distinct analytes
# (for example direct bilirubin, creatinine clearance, and HbA1c) into a
# different clinical value. New fields use _LOINC_LAB_FIELDS instead.
_LEGACY_LAB_CONCEPT_FIELDS = {
    'hemoglobin': 'hemoglobin_level',
    'hemoglobin measurement': 'hemoglobin_level',
    'platelet count': 'platelet_count',
    'creatinine': 'serum_creatinine_level',
    'creatinine in serum': 'serum_creatinine_level',
    'calcium': 'serum_calcium_level',
    'bilirubin total': 'serum_bilirubin_level_total',
    'bilirubin.total [mass/volume] in serum or plasma': 'serum_bilirubin_level_total',
    'albumin': 'albumin_level',
}

_BEHAVIOR_MEASUREMENT_FIELDS = {
    '72166-2': ('smoking_status', str),
    '63640-7': ('pack_years', float),
    '74013-4': ('alcohol_use', str),
    '11286-7': ('drinks_per_week', int),
    '68516-4': ('exercise_frequency', str),
    '89555-7': ('exercise_minutes_per_week', int),
    '88365-2': ('diet_type', str),
    '93831-6': ('sleep_quality', str),
    '73985-4': ('stress_level', str),
    '93033-9': ('social_support', str),
    '74165-2': ('employment_status', str),
    '82589-3': ('education_level', str),
    '45404-1': ('marital_status', str),
    '76513-1': ('insurance_type', str),
    '63512-8': ('number_of_dependents', int),
    '77243-3': ('annual_household_income', int),
}


# #505 assertion registry.  The importer represents FHIR Observation answers
# as OMOP Measurement rows; native OMOP producers may use Observation instead.
# The registry intentionally accepts only these reviewed LOINC questions.  A
# missing row is unknown, and an arbitrary string or concept-name match cannot
# accidentally turn into a clinical assertion.
#
# (PatientRecord field, value kind).  ``inverse_boolean`` is for questions
# whose answer describes the *presence* of a risk whereas the projection says
# ``no_*``.
_ASSERTION_FIELDS = {
    '2106-3': ('pregnancy_test_result_value', 'string'),
    '8659-8': ('contraceptive_use', 'boolean'),
    '75985-6': ('consent_capability', 'boolean'),
    '74014-2': ('caregiver_availability_status', 'boolean'),
    '75618-3': ('no_mental_health_disorder_status', 'inverse_boolean'),
    '74204-0': ('no_substance_use_status', 'inverse_boolean'),
    '82593-5': ('no_geographic_exposure_risk', 'inverse_boolean'),
}


def _clear_derived_fields(patient_info: PatientRecord) -> None:
    """Reset all OMOP-derived fields to None so deletions are reflected."""
    for field in _OMOP_DERIVED_FIELDS:
        if hasattr(patient_info, field):
            default = [] if field in (
                'prior_procedures', 'later_therapies', 'genetic_mutations', 'later_therapy_ids',
                'first_line_component_ids', 'second_line_component_ids',
                'later_component_ids', 'therapy_component_ids',
                'first_line_therapy_type_ids', 'second_line_therapy_type_ids',
                'later_therapy_type_ids', 'therapy_type_ids',
                'stem_cell_transplant_history', 'sct_eligibility',
            ) else None
            setattr(patient_info, field, default)


def _is_empty(value) -> bool:
    """True for the values derivation leaves behind when it finds no source data.

    Deliberately not falsy-testing: 0 (ECOG 0, zero relapses) and False (a
    recorded negative) are real derived answers, not absences.
    """
    return value is None or value == '' or value == [] or value == {}


@dataclasses.dataclass(frozen=True)
class OmopSnapshot:
    """Pre-fetched OMOP rows for a single person.

    Built once per refresh call; passed to every section extractor so each table
    is queried at most once instead of 10-15 times.  Indexes (``meas_by_code``,
    ``obs_by_code``, ``meas_by_source``, ``obs_by_source``) give O(1) lookup by
    concept code or source value.
    """
    measurements: list          # non-erroneous, -date -id, select_related(concept, value_as_concept, qualifier_concept)
    observations: list          # non-erroneous, -date -id, select_related(concept, value_as_concept, vocabulary)
    conditions: list            # all, -start_date, select_related(concept, status_concept)
    drug_exposures: list        # all, start_date id, select_related(drug_concept)
    procedures: list            # all, -date, select_related(procedure_concept)
    death: object | None        # Death row or None
    # Computed indexes for O(1) lookup:
    meas_by_code: dict          # concept_code → [Measurement]
    obs_by_code: dict           # concept_code → [Observation]
    meas_by_source: dict        # source_value → [Measurement]
    obs_by_source: dict         # source_value → [Observation]


def _build_snapshot(person: Person) -> OmopSnapshot:
    """Fetch all OMOP rows for *person* in ~6 SQL queries and build lookup indexes."""
    measurements = list(
        Measurement.objects
        .filter(person=person, is_erroneous=False)
        .select_related(
            'measurement_concept', 'value_as_concept',
            'qualifier_concept', 'measurement_concept__vocabulary',
        )
        .order_by('-measurement_date', '-measurement_id')
    )
    observations = list(
        Observation.objects
        .filter(person=person, is_erroneous=False)
        .select_related(
            'observation_concept', 'value_as_concept',
            'observation_concept__vocabulary',
        )
        .order_by('-observation_date', '-observation_id')
    )
    conditions = list(
        ConditionOccurrence.objects
        .filter(person=person)
        .select_related('condition_concept', 'condition_status_concept')
        .order_by('-condition_start_date')
    )
    drug_exposures = list(
        DrugExposure.objects
        .filter(person=person)
        .select_related('drug_concept')
        .order_by('drug_exposure_start_date', 'drug_exposure_id')
    )
    procedures = list(
        ProcedureOccurrence.objects
        .filter(person=person)
        .select_related('procedure_concept')
        .order_by('-procedure_date')
    )
    death = Death.objects.filter(person=person).only('death_date').first()

    # Build code/source indexes
    meas_by_code: dict[str, list] = defaultdict(list)
    meas_by_source: dict[str, list] = defaultdict(list)
    for m in measurements:
        cc = getattr(m.measurement_concept, 'concept_code', None)
        if cc:
            meas_by_code[cc].append(m)
        sv = m.measurement_source_value
        if sv:
            meas_by_source[sv].append(m)

    obs_by_code: dict[str, list] = defaultdict(list)
    obs_by_source: dict[str, list] = defaultdict(list)
    for o in observations:
        cc = getattr(o.observation_concept, 'concept_code', None)
        if cc:
            obs_by_code[cc].append(o)
        sv = o.observation_source_value
        if sv:
            obs_by_source[sv].append(o)

    return OmopSnapshot(
        measurements=measurements,
        observations=observations,
        conditions=conditions,
        drug_exposures=drug_exposures,
        procedures=procedures,
        death=death,
        meas_by_code=dict(meas_by_code),
        obs_by_code=dict(obs_by_code),
        meas_by_source=dict(meas_by_source),
        obs_by_source=dict(obs_by_source),
    )


def refresh_patient_record(person: Person) -> PatientRecord:
    """Derive and upsert PatientRecord from OMOP tables for a given person.

    This is the single source of truth for PatientRecord derivation. It is called
    by:
      - The populate_patient_record management command
      - OMOP post_save signals (ConditionOccurrence, DrugExposure, Measurement, etc.)
      - The FHIR upload endpoint (after writing OMOP records)
    """
    with transaction.atomic():
        try:
            patient_info = PatientRecord.objects.select_for_update().get(person=person)
        except PatientRecord.DoesNotExist:
            patient_info = PatientRecord(person=person)

        # Clear all OMOP-derived fields before re-deriving so deletions are reflected.
        _clear_derived_fields(patient_info)

        # Pre-fetch all OMOP rows once (~6 queries) instead of per-section.
        snapshot = _build_snapshot(person)

        # Populate all sections
        for section_fn in [
            _get_demographics,
            _get_location_data,
            _get_disease_data,
            _get_treatment_data,
            _get_vitals_data,
            _get_biomarker_data,
            _get_genomics_pathology_data,
            _get_staging_data,
            _get_social_data,
            _get_behavior_data,
            _get_assertion_data,
            _get_infection_data,
            _get_assessment_data,
            _get_laboratory_data,
            _get_performance_data,
            _get_genetic_mutations,
            _get_tumor_size_data,
            _get_cll_data,
            _get_lymphoma_data,
            _get_bc_clinical_data,
            _get_prior_procedures,
            _get_wearable_data,
            _get_mm_specific_data,
            _get_sct_cytogenetic_data,
        ]:
            for field, value in section_fn(person, snapshot).items():
                setattr(patient_info, field, value)

        _compute_derived_fields(patient_info)

        patient_info.derivation_version = DERIVATION_VERSION
        patient_info.derived_at = timezone.now()

        patient_info.save()
        return patient_info


# ---------------------------------------------------------------------------
# Section extractors — each returns a dict of {field_name: value}
# ---------------------------------------------------------------------------

def _get_demographics(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    if snapshot.death:
        data['death_date'] = snapshot.death.death_date

    if person.year_of_birth not in PERSON_YEAR_PLACEHOLDERS:
        try:
            data['date_of_birth'] = date(
                person.year_of_birth,
                person.month_of_birth or 1,
                person.day_of_birth or 1,
            )
        except ValueError:
            # OMOP permits partial dates and some imported rows contain an
            # invalid partial month/day. Keep the known year rather than
            # discarding the patient's age and DOB entirely.
            data['date_of_birth'] = date(person.year_of_birth, 1, 1)

    gender_src = None
    if person.gender_concept:
        gender_src = person.gender_concept.concept_name.lower()
    elif person.gender_source_value:
        gender_src = person.gender_source_value.lower()
    if gender_src:
        if 'male' in gender_src and 'female' not in gender_src:
            data['gender'] = 'M'
        elif 'female' in gender_src:
            data['gender'] = 'F'
        else:
            data['gender'] = 'U'

    if person.race_concept and person.race_concept.concept_id != 0:
        data['race'] = person.race_concept.concept_name
    elif person.race_source_value and person.race_source_value != 'unknown':
        data['race'] = person.race_source_value
    if person.ethnicity_concept and person.ethnicity_concept.concept_id != 0:
        data['ethnicity'] = person.ethnicity_concept.concept_name
    elif person.ethnicity_source_value and person.ethnicity_source_value != 'unknown':
        data['ethnicity'] = person.ethnicity_source_value

    lang_skills = person.language_skills.select_related('language_concept').all()
    if lang_skills.exists():
        parts = [
            f'{ls.language_concept.concept_name}: {ls.skill_level}'
            for ls in lang_skills
        ]
        data['languages_skills'] = ', '.join(parts)

    data.update({
        'email': person.email,
        'phone_number': person.phone_number,
        'facility_name': person.facility_name,
        'validated': person.validated,
        'validated_by': person.validated_by,
        'validation_date': person.validation_date,
        'suppress_demographics_for_others': person.suppress_demographics_for_others,
    })

    return data


def _get_location_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    # No snapshot usage — Location is a Person FK, not an OMOP scan.

    if person.location_id:
        try:
            location = Location.objects.get(location_id=person.location_id)
            data.update({
                'country': location.country,
                'region': location.state,
                'city': location.city,
                'postal_code': location.zip,
                'latitude': float(location.latitude) if location.latitude else None,
                'longitude': float(location.longitude) if location.longitude else None,
            })
        except Location.DoesNotExist:
            pass

    return data


# Clinical-interpretation aliases: raw OMOP condition concept names remapped to
# the canonical disease titles PRomop uses (ADR 0001).
# Keyed by lowercased concept name; only exact matches are remapped, so
# unrelated conditions pass through untouched.
_DISEASE_ALIASES = {
    'myeloma': 'multiple myeloma',
    # Breast cancer — all common OMOP/SNOMED surface forms → single canonical title
    'breast cancer': 'Breast Cancer',
    'breast cancer (disorder)': 'Breast Cancer',
    'malignant neoplasm of breast': 'Breast Cancer',
    'malignant neoplasm of breast (disorder)': 'Breast Cancer',
    'carcinoma of breast': 'Breast Cancer',
    'carcinoma of breast (disorder)': 'Breast Cancer',
    'er|erbb2 breast cancer': 'Breast Cancer',
    'er|erbb2 breast cancer (disorder)': 'Breast Cancer',
}


def _canonicalize_disease(name: str) -> str:
    """Map a raw OMOP condition concept name to EXACT's canonical disease title.

    Unrecognised names are returned unchanged (preserve, don't drop).
    """
    if not name:
        return name
    normalized = name.strip().lower()
    return _DISEASE_ALIASES.get(normalized, name)


# EXACT's canonical trial disease vocabulary (lowercased). These are the SUBMITTED values
# CB stores in PatientInfo.disease and in trials_trial.disease (the matcher compares them
# with disease__iexact) — i.e. the keys of CB's disease_utils.DISEASE_TITLE_TO_CODE, NOT the
# display labels ("Multiple Myeloma"). The CB profile-write path stores that value verbatim in
# condition_source_value. When a source value canonicalizes into this set it is a CB-authored
# disease and is preferred over the mapped concept_name in `_get_disease_data`, because a
# standard Condition concept can be a status/subtype variant (e.g. "Multiple myeloma in
# remission") that would not match the trial strings. FHIR-imported rows (source value = a code
# / free text) are not in this set and keep the existing concept-first behavior. Kept in sync
# with CB's disease vocabulary by hand (omop_core cannot import CB). [CB #625a, preserved
# through the dev back-merge]
_CANONICAL_DISEASES = frozenset({
    'multiple myeloma', 'follicular lymphoma', 'breast cancer',
    'chronic lymphocytic leukemia', 'mantle cell lymphoma',
})


def _get_disease_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    # Most-recent oncologic condition — match common OMOP oncology terms
    # in either the mapped concept_name OR the original condition_source_value
    # (concept_id=0 rows store the disease name only in source_value).
    _ONCO_KEYWORDS = [
        'cancer', 'neoplasm', 'malignant', 'lymphoma', 'leukemia',
        'myeloma', 'carcinoma', 'sarcoma', 'tumor',
    ]

    def _is_oncologic(cond):
        cname = (cond.condition_concept.concept_name or '').lower() if cond.condition_concept else ''
        src = (cond.condition_source_value or '').lower()
        return any(kw in cname or kw in src for kw in _ONCO_KEYWORDS)

    # snapshot.conditions is already ordered -condition_start_date
    cancer_condition = next((c for c in snapshot.conditions if _is_oncologic(c)), None)

    if cancer_condition:
        # Prefer source_value when concept is unmapped (id=0 / sentinel). [dev's helper]
        concept_name = _usable_concept_name(cancer_condition.condition_concept) if cancer_condition.condition_concept_id else None
        # CB #625a (preserved through the dev back-merge): a CB-authored profile edit stores the
        # canonical disease title verbatim in condition_source_value. The mapped concept_name can
        # be a status/subtype variant ("Multiple myeloma in remission") that does not match
        # EXACT's trial disease vocabulary, so when the source value IS a recognized canonical
        # disease, prefer it over the concept name. Non-CB rows (FHIR imports: source value is a
        # code or free text) are not recognized and keep the concept-first behavior.
        src = (cancer_condition.condition_source_value or '').strip()
        if src and _canonicalize_disease(src).lower() in _CANONICAL_DISEASES:
            raw_name = src
        else:
            raw_name = concept_name or src or ''
        if raw_name:
            data['disease'] = _canonicalize_disease(raw_name)
        if cancer_condition.condition_start_date:
            data['diagnosis_date'] = cancer_condition.condition_start_date

    # Any condition for diagnosis_date fallback (earliest)
    if 'diagnosis_date' not in data:
        dated = [c for c in snapshot.conditions if c.condition_start_date]
        if dated:
            earliest = min(dated, key=lambda c: c.condition_start_date)
            data['diagnosis_date'] = earliest.condition_start_date

    # condition_clinical_status — from condition_status_concept
    # snapshot.conditions is -start_date ordered, so first is most recent
    most_recent_condition = snapshot.conditions[0] if snapshot.conditions else None

    if most_recent_condition:
        if most_recent_condition.condition_status_concept:
            status_name = most_recent_condition.condition_status_concept.concept_name.lower()
        elif most_recent_condition.condition_status_source_value:
            status_name = most_recent_condition.condition_status_source_value.lower()
        else:
            status_name = None
        if status_name:
            if 'remission' in status_name:
                data['condition_clinical_status'] = 'remission'
            elif 'relapse' in status_name or 'recur' in status_name or 'recurrence' in status_name:
                data['condition_clinical_status'] = 'relapse'
            elif 'active' in status_name:
                data['condition_clinical_status'] = 'active'
            elif 'resolved' in status_name or 'inactive' in status_name:
                data['condition_clinical_status'] = 'resolved'
            else:
                data['condition_clinical_status'] = status_name[:50]

    # disease_slug — machine-readable ID derived from disease name
    disease_name = data.get('disease', '')
    if disease_name:
        data['disease_slug'] = _disease_name_to_slug(disease_name)

    return data


def _disease_name_to_slug(name: str) -> str:
    """Convert a disease concept name to a URL-friendly slug."""
    import re
    slug = name.lower()
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    slug = slug.strip('-')
    return slug[:100]


def _get_treatment_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    drug_exposures = snapshot.drug_exposures  # already ordered by start_date, id

    recent_drugs = list(reversed(drug_exposures[-10:]))

    current_meds = []
    for drug in recent_drugs[:5]:
        name = _usable_concept_name(drug.drug_concept) or drug.drug_source_value
        if name:
            current_meds.append(name)
    if current_meds:
        data['concomitant_medications'] = ', '.join(current_meds)

    # Try Episode-based therapy line grouping first
    try:
        from omop_oncology.models import Episode
        episodes = Episode.objects.filter(person=person).select_related(
            'episode_source_concept', 'episode_object_concept').order_by('episode_number')
        if episodes.exists():
            return _get_treatment_data_from_episodes(person, data, episodes, drug_exposures, snapshot)
    except Exception:
        pass

    # No Episodes persisted yet — derive from the single LOT inference engine in
    # read-only (dry_run) mode, so the same grouping algorithm the enrich/import
    # steps use to *persist* Episodes also drives derivation. No OMOP rows are
    # written here; refresh_patient_record stays read-only.
    if not drug_exposures:
        return data

    from omop_core.services.lot_inference_service import infer_lot_for_person
    # The snapshot already has the rows LOT inference needs. Reuse them rather
    # than querying DrugExposure and ProcedureOccurrence a second time.
    lots = infer_lot_for_person(
        person,
        force=True,
        dry_run=True,
        exposures=drug_exposures,
        procedures=snapshot.procedures,
    )
    _apply_inferred_lots(data, lots, drug_exposures=drug_exposures)
    return data


# HemOnc regimen→component relationship ids. Direction: concept_id_1 is the
# HemOnc Regimen concept, concept_id_2 is a component drug concept.
_COMPONENT_RELATIONSHIP_IDS = (
    'Has cytotoxic chemo', 'Has targeted therapy', 'Has immunotherapy',
    'Has steroid tx', 'Has hormonal tx',
)

# Relationships used to level any drug concept to its standard/ingredient
# forms so consumers (EXACT/SoC) can match by plain concept_id overlap (#189):
# 'Maps to' reaches the standard concept (e.g. HemOnc drug → RxNorm), and
# 'Has ingredient' reaches ingredient granularity (clinical drug → ingredient,
# matching EXACT's TherapyComponent.omop_concept_id leveling).
_LEVELING_RELATIONSHIP_IDS = ('Maps to', 'Has ingredient')


def _expand_component_ids(regimen_concept_ids, drug_concept_ids):
    """Expand one therapy line's inputs into a set of component drug concept_ids.

    regimen_concept_ids: HemOnc regimen concept_ids for the line (may be empty).
    drug_concept_ids: concept_ids from the line's backing DrugExposure rows.

    The returned set is the union of:
      1. HemOnc components of the regimen(s) via _COMPONENT_RELATIONSHIP_IDS,
      2. the exposure drug concept_ids themselves,
      3. 'Maps to' / 'Has ingredient' targets of (1) and (2) — ingredient leveling,
      4. 'Has ingredient' targets of the 'Maps to' targets from (3), covering
         HemOnc drug → RxNorm clinical drug → ingredient.

    At most 3 batched queries regardless of input size.
    """
    base = set()
    regimen_ids = {int(r) for r in (regimen_concept_ids or ()) if r}
    if regimen_ids:
        base.update(
            ConceptRelationship.objects.filter(
                concept_1_id__in=regimen_ids,
                relationship_id__in=_COMPONENT_RELATIONSHIP_IDS,
            ).values_list('concept_2_id', flat=True)
        )
    base.update(int(d) for d in (drug_concept_ids or ()) if d)
    if not base:
        return set()

    hop1 = set(
        ConceptRelationship.objects.filter(
            concept_1_id__in=base,
            relationship_id__in=_LEVELING_RELATIONSHIP_IDS,
        ).values_list('concept_2_id', flat=True)
    )
    hop2 = set()
    if hop1:
        hop2 = set(
            ConceptRelationship.objects.filter(
                concept_1_id__in=hop1,
                relationship_id='Has ingredient',
            ).values_list('concept_2_id', flat=True)
        )
    return base | hop1 | hop2


def _latest_published_release_id():
    """The current published vocabulary release identity as a string, or None.

    Stamped into therapy-id provenance so a consumer knows which vocabulary
    release a regimen concept_id was resolved against. Returned as a **string** to
    match the documented API contract / frontend type (`lines_of_therapy.release_id`
    is a string). Until a content-addressed release id exists (ADR 0001), the value
    is the decimal string of the VocabularyRelease pk (e.g. "7"), not yet the
    `rel-...` shape the field comment shows as the eventual target.
    """
    try:
        from django.db.models import F
        from omop_core.models import VocabularyRelease
        pk = (VocabularyRelease.objects
              .filter(status='published')
              # published_at is nullable; nulls_last so a stray null-timestamp
              # published row can't sort ahead of a real one under DESC.
              .order_by(F('published_at').desc(nulls_last=True))
              .values_list('pk', flat=True)
              .first())
        return str(pk) if pk is not None else None
    except Exception:
        return None


def _is_asserted_regimen(concept) -> bool:
    """True only for a source-asserted HemOnc *regimen* concept — the one case a
    consumer is told it may trust without re-verifying.

    Requires a HemOnc, Regimen-class, standard, non-invalid concept. A HemOnc
    concept that is a component drug (not a Regimen), non-standard, or retired is
    NOT asserted: its id may still be used as the line's regimen, but the origin
    is reported as 'inferred' so a consumer verifies it. Guards the dangerous
    inferred→asserted direction.
    """
    return bool(
        concept is not None
        and getattr(concept, 'vocabulary_id', None) == 'HemOnc'
        and getattr(concept, 'concept_class_id', None) == 'Regimen'
        and getattr(concept, 'standard_concept', None) == 'S'
        and not getattr(concept, 'invalid_reason', None)
    )


def _prov_entry(concept_id, origin, release_id):
    """A therapy_ids_provenance entry, or None when there is nothing to record.

    origin is 'asserted' only when the source supplied a validated HemOnc regimen
    concept via Episode.episode_source_concept; otherwise 'inferred' (resolved by
    matching the line's drug-name set, or from a text/synthetic exposure backfill).
    """
    if not concept_id:
        return None
    return {'value': concept_id, 'origin': origin, 'release_id': release_id}
# HemOnc concept_class_id for drug-class ("type") concepts, e.g. Proteasome
# inhibitor / IMiD / anti-CD38. A drug's class membership is a 'Is a' edge
# (HemOnc Component --[Is a]--> Component Class); classes themselves chain to
# broader classes by the same edge. NOT concept_ancestor — a class's ancestry
# there is the regimens that *use* it, not its member drugs (ADR 0002 Phase 0).
_TYPE_CONCEPT_CLASS_ID = 'Component Class'
_TYPE_RELATIONSHIP_ID = 'Is a'
# Depth backstop for the class BFS. Real HemOnc class chains are ~2-3 deep
# (drug → narrow class → broad class); the cap only guards pathological graphs.
_CLASS_MAX_HOPS = 5


def _expand_class_ids(component_ids):
    """Derive therapy-class ("type") concept_ids for one line's components.

    Given the line's component drug concept_ids (as produced by
    _expand_component_ids — which includes the HemOnc Component seeds plus any
    'Maps to'/'Has ingredient'-leveled RxNorm concepts), follow HemOnc
    'Component --[Is a]--> Component Class' edges transitively, keeping only
    targets whose concept_class_id is 'Component Class' (the drug-class
    concepts). Non-class concepts in the input (e.g. leveled RxNorm ingredients)
    simply contribute no 'Is a → Component Class' edges and fall away. Sub-class
    chains (narrow class --Is a--> broader class) are followed so a patient
    carries *every* applicable class, not just the narrowest — matching how
    trial type criteria are authored.

    Bounded BFS: one query per hop, a visited set prevents cycles, and
    _CLASS_MAX_HOPS backstops any pathological graph. Returns a set of
    class concept_ids (never includes the seed component ids themselves).
    """
    def _class_parents(ids):
        # Exclude retired edges and retired class targets: Athena preserves
        # invalid_reason on historical rows, and an obsolete class id in a
        # patient's set would produce a false type-criterion match downstream.
        return set(
            ConceptRelationship.objects.filter(
                concept_1_id__in=ids,
                relationship_id=_TYPE_RELATIONSHIP_ID,
                concept_2__concept_class_id=_TYPE_CONCEPT_CLASS_ID,
                invalid_reason__isnull=True,
                concept_2__invalid_reason__isnull=True,
            ).values_list('concept_2_id', flat=True)
        )

    seed_size = len({int(c) for c in (component_ids or ()) if c})
    if not seed_size:
        return set()
    seen = {int(c) for c in (component_ids or ()) if c}
    classes = set()
    frontier = seen
    for _ in range(_CLASS_MAX_HOPS):
        new = _class_parents(frontier) - seen
        if not new:
            return classes          # graph fully resolved within the cap
        classes |= new
        seen |= new
        frontier = new
    # Cap reached with a non-empty frontier. Probe one more hop to tell
    # "chain ended exactly at the cap" (complete) from a genuine truncation —
    # only the latter is worth a warning. Never drop silently (project
    # convention): surface real truncations for triage.
    if _class_parents(frontier) - seen:
        logger.warning(
            "therapy-class BFS hit depth cap _CLASS_MAX_HOPS=%d with deeper "
            "'Is a' → Component Class ancestors still unexpanded (seed size %d, "
            "classes so far %d)",
            _CLASS_MAX_HOPS, seed_size, len(classes),
        )
    return classes


def _regimen_from_exposures(exposure_ids, de_info_by_id):
    """Resolve (display_name, concept_id, origin) from a LOT's backing DrugExposures.

    de_info_by_id maps drug_exposure_id -> (concept_id, vocabulary_id, name).

    This is the no-Episode inference path, so origin is 'inferred' for any
    resolved concept_id (None when nothing resolves) — never 'asserted'. Even a
    HemOnc regimen concept sitting on a DrugExposure is not a source assertion
    here: those rows can come from a text/synthetic backfill (e.g. breast-cancer
    enrichment, fill_org_analytics_gaps), not from the source's own regimen code.
    Only the Episode path's episode_source_concept is treated as asserted.

    If an exposure's drug_concept is itself a HemOnc concept, use it directly;
    otherwise a regimen resolved from the drug-name set, else the canonical
    regimen name, else the joined original concept names (preserving casing).
    """
    infos = [de_info_by_id[e] for e in exposure_ids if e in de_info_by_id]
    if not infos:
        return 'Unknown', None, None
    for concept_id, vocab_id, name in infos:
        if concept_id and vocab_id in ('HemOnc', 'HK-Regimen'):
            return name, concept_id, 'inferred'
    display_names = [name for _, _, name in infos]
    norm_key = {normalize_drug_name(n).lower().strip() for n in display_names}
    concept_id = get_regimen_concept_id(norm_key)
    if concept_id:
        concept = _cc_by_id(concept_id)
        if concept:
            return concept.concept_name, concept_id, 'inferred'
    regimen_name = get_regimen_name(norm_key)
    if regimen_name:
        return regimen_name, concept_id, ('inferred' if concept_id else None)
    return ' + '.join(display_names), concept_id, ('inferred' if concept_id else None)


def _apply_inferred_lots(data: dict, lots, drug_exposures=None) -> None:
    """Map in-memory inferred LOTs onto first/second/later therapy fields."""
    data['therapy_lines_count'] = len(lots)

    # Bulk-resolve (concept_id, vocabulary_id, name) for every exposure across
    # all lots. concept.vocabulary_id is the FK's string PK — no extra join.
    exp_ids = [eid for lot in lots for eid in lot.exposure_ids]
    exp_id_set = set(exp_ids)
    de_info_by_id = {}
    if exp_ids:
        exposures = drug_exposures
        if exposures is None:
            exposures = (
                DrugExposure.objects
                .filter(drug_exposure_id__in=exp_ids)
                .select_related('drug_concept')
            )
        for de in exposures:
            if de.drug_exposure_id not in exp_id_set:
                continue
            name = _usable_concept_name(de.drug_concept)
            if name:
                de_info_by_id[de.drug_exposure_id] = (
                    de.drug_concept.concept_id,
                    de.drug_concept.vocabulary_id,
                    name,
                )
            else:
                de_info_by_id[de.drug_exposure_id] = (None, None, de.drug_source_value or 'Unknown')

    release_id = _latest_published_release_id()
    prov: dict = {}
    later = []
    later_ids = []
    later_origins = []
    later_components = set()
    all_components = set()
    later_classes = set()
    all_classes = set()
    for lot in lots:
        name, concept_id, origin = _regimen_from_exposures(lot.exposure_ids, de_info_by_id)
        exposure_concept_ids = [
            de_info_by_id[e][0]
            for e in lot.exposure_ids
            if e in de_info_by_id and de_info_by_id[e][0]
        ]
        components = _expand_component_ids(
            [concept_id] if concept_id else [], exposure_concept_ids,
        )
        all_components |= components
        classes = _expand_class_ids(components)
        all_classes |= classes
        start = str(lot.start) if lot.start else None
        end = str(lot.end) if lot.end else None
        if lot.lot_number == 1:
            data['first_line_therapy'] = name
            data['first_line_therapy_id'] = concept_id
            data['first_line_component_ids'] = sorted(components)
            data['first_line_therapy_type_ids'] = sorted(classes)
            data['first_line_date'] = start
            data['first_line_start_date'] = start
            data['first_line_end_date'] = end
            if concept_id:
                prov['first_line_therapy_id'] = _prov_entry(concept_id, origin, release_id)
        elif lot.lot_number == 2:
            data['second_line_therapy'] = name
            data['second_line_therapy_id'] = concept_id
            data['second_line_component_ids'] = sorted(components)
            data['second_line_therapy_type_ids'] = sorted(classes)
            data['second_line_date'] = start
            data['second_line_start_date'] = start
            data['second_line_end_date'] = end
            if concept_id:
                prov['second_line_therapy_id'] = _prov_entry(concept_id, origin, release_id)
        else:  # lot_number >= 3
            later_components |= components
            later_classes |= classes
            if not data.get('later_therapy'):
                data['later_therapy'] = name
                data['later_date'] = start
                data['later_start_date'] = start
                data['later_end_date'] = end
            # Carry the actual line number, per-line concept_id (may be None when
            # the regimen did not resolve) and origin so the structured
            # lines_of_therapy payload keeps the true line number, pairs each
            # later line with its own id, and reports per-line asserted/inferred.
            later.append({'lineNumber': lot.lot_number, 'therapy': name,
                          'startDate': start, 'endDate': end,
                          'concept_id': concept_id, 'origin': origin})
            if concept_id:
                later_ids.append(concept_id)
                later_origins.append(origin)
    if later:
        data['later_therapies'] = later
    if later_ids:
        data['later_therapy_ids'] = later_ids
        # Field-level provenance is a single origin for the aggregate list, so
        # report 'asserted' only when every resolved later line is asserted —
        # never over-claim assertion for a mixed set. Per-line origins live in
        # later_therapies for the payload's per-line regimen_source.
        _later_origin = 'asserted' if later_origins and all(o == 'asserted' for o in later_origins) else 'inferred'
        prov['later_therapy_ids'] = {'value': later_ids, 'origin': _later_origin, 'release_id': release_id}
    if later_components:
        data['later_component_ids'] = sorted(later_components)
    if all_components:
        data['therapy_component_ids'] = sorted(all_components)
    if prov:
        data['therapy_ids_provenance'] = prov
    if later_classes:
        data['later_therapy_type_ids'] = sorted(later_classes)
    if all_classes:
        data['therapy_type_ids'] = sorted(all_classes)


def _get_treatment_data_from_episodes(person, data, episodes, drug_exposures, snapshot: OmopSnapshot = None):
    """Use Episode records for structured therapy line grouping."""
    try:
        from omop_oncology.models import EpisodeEvent
        from omop_core.services.lot_regimens import get_regimen_concept_id
    except ImportError:
        return data

    # ── Bulk-fetch EpisodeEvents and DrugExposures for all episodes ────────
    # Replaces per-episode queries with 2 total queries.
    all_episode_ids = [e.episode_id for e in episodes]

    ee_by_episode: dict = {}
    for ee in EpisodeEvent.objects.filter(episode_id__in=all_episode_ids).values('episode_id', 'event_id'):
        ee_by_episode.setdefault(ee['episode_id'], []).append(ee['event_id'])

    all_event_ids = [eid for ids in ee_by_episode.values() for eid in ids]
    de_by_id: dict = {
        de.drug_exposure_id: de
        for de in DrugExposure.objects.filter(drug_exposure_id__in=all_event_ids).select_related('drug_concept')
    } if all_event_ids else {}

    # ── First pass: compute drug name sets and regimen concept_ids ─────────
    # No additional DB queries here — episode_source_concept already loaded via select_related.
    episode_rows: list = []  # (episode, drugs_in_episode, concept_id)
    needed_concept_ids: set = set()

    for episode in episodes:
        event_ids = ee_by_episode.get(episode.episode_id, [])
        drugs_in_episode = [de_by_id[eid] for eid in event_ids if eid in de_by_id]

        drug_name_set = {
            normalize_drug_name(n)
            for de in drugs_in_episode
            for n in [_usable_concept_name(de.drug_concept)]
            if n
        }
        # Drug source values (regimen names set by import handler) as fallback
        source_value_set = {
            de.drug_source_value.strip()
            for de in drugs_in_episode
            if de.drug_source_value and de.drug_source_value.strip()
        }

        # Prefer HemOnc concept already joined via select_related; if the attached
        # concept is from a different vocabulary (e.g. a generic Drug domain fallback),
        # it does not represent the MM regimen — skip it and resolve from drug names.
        concept_id = None
        origin = None
        src_concept = episode.episode_source_concept
        if (episode.episode_source_concept_id and src_concept and
                getattr(src_concept, 'vocabulary_id', None) == 'HemOnc'):
            concept_id = episode.episode_source_concept_id
            # Only a validated HemOnc Regimen concept is 'asserted'; a HemOnc
            # component / non-standard / retired source concept keeps the id but
            # is reported 'inferred' — never over-claim assertion.
            origin = 'asserted' if _is_asserted_regimen(src_concept) else 'inferred'
        elif drug_name_set:
            concept_id = get_regimen_concept_id(drug_name_set)
            if concept_id:
                origin = 'inferred'  # resolved by matching the line's drug names
        # Fallback: treat each drug_source_value as an abbreviated regimen name
        if not concept_id and source_value_set:
            for sv in source_value_set:
                cid = get_regimen_concept_id_by_name(sv)
                if cid:
                    concept_id = cid
                    origin = 'inferred'
                    break
        # Last resort: an enrichment-built episode carries the (derived) regimen in
        # episode_object_concept, not the source slot (#362). When the name fallbacks
        # can't recover it — e.g. a FHIR regimen with a valid concept_id but a display
        # name outside the alias table — take it from the object slot so the record
        # doesn't silently lose its regimen id. Always 'inferred' (object ≠ a source
        # assertion), never 'asserted'.
        if not concept_id:
            obj = episode.episode_object_concept
            if (episode.episode_object_concept_id and obj
                    and getattr(obj, 'vocabulary_id', None) == 'HemOnc'
                    and getattr(obj, 'concept_class_id', None) == 'Regimen'):
                concept_id = episode.episode_object_concept_id
                origin = 'inferred'

        if concept_id:
            needed_concept_ids.add(concept_id)
        episode_rows.append((episode, drugs_in_episode, concept_id, source_value_set, origin))

    # ── Bulk-fetch Concept names in one query ──────────────────────────────
    # Concept rows already loaded via select_related are re-used directly.
    # Filter the sentinel so concept 0 / 'No matching concept' never reaches
    # therapy labels (#480, follow-up to #458).
    concept_name_map: dict = {
        ep.episode_source_concept_id: name
        for ep in episodes
        if ep.episode_source_concept_id and ep.episode_source_concept
        for name in [_usable_concept_name(ep.episode_source_concept)]
        if name
    }
    to_fetch = needed_concept_ids - set(concept_name_map)
    if to_fetch:
        concept_name_map.update(
            (c.concept_id, name)
            for c in Concept.objects.filter(concept_id__in=to_fetch).only('concept_id', 'concept_name')
            for name in [_usable_concept_name(c)]
            if name
        )

    # ── Second pass: populate data dict ───────────────────────────────────
    release_id = _latest_published_release_id()
    prov: dict = {}
    later_origins = []
    therapy_line_numbers = set()
    later_components = set()
    all_components = set()
    later_classes = set()
    all_classes = set()

    for episode, drugs_in_episode, concept_id, source_value_set, origin in episode_rows:
        lot = episode.episode_number
        if lot is None:
            continue
        therapy_line_numbers.add(lot)

        # Nullify dangling FK concept_ids (not in Concept table)
        if concept_id and concept_id not in concept_name_map:
            concept_id = None
            origin = None

        # Component drug concept_ids for this line: HemOnc regimen expansion
        # unioned with the line's exposure drug concepts, leveled to ingredients.
        components = _expand_component_ids(
            [concept_id] if concept_id else [],
            [de.drug_concept_id for de in drugs_in_episode if de.drug_concept_id],
        )
        all_components |= components
        # Therapy-class ("type") concept_ids for this line (ADR 0002).
        classes = _expand_class_ids(components)
        all_classes |= classes

        if concept_id:
            drug_names = concept_name_map[concept_id]
        else:
            # Try regimen name resolution: drug concept names first, then source values
            from omop_core.services.lot_regimens import get_regimen_name
            _drug_cnames = [normalize_drug_name(n) for de in drugs_in_episode for n in [_usable_concept_name(de.drug_concept)] if n]
            _regimen_name = get_regimen_name({n.lower().strip() for n in _drug_cnames}) if _drug_cnames else None
            if not _regimen_name:
                for sv in source_value_set:
                    _regimen_name = get_regimen_name({sv.lower()})
                    if _regimen_name:
                        break
            drug_names = (
                _regimen_name
                or next(iter(source_value_set), None)  # raw regimen name from drug_source_value
                or ' + '.join(_drug_cnames)
                or 'Unknown'
            )

        start_date = str(episode.episode_start_date) if episode.episode_start_date else None
        end_date = str(episode.episode_end_date) if episode.episode_end_date else None

        if lot == 1:
            data['first_line_therapy'] = drug_names
            data['first_line_therapy_id'] = concept_id
            data['first_line_component_ids'] = sorted(components)
            data['first_line_therapy_type_ids'] = sorted(classes)
            data['first_line_date'] = start_date
            data['first_line_start_date'] = start_date
            if end_date:
                data['first_line_end_date'] = end_date
            if concept_id:
                prov['first_line_therapy_id'] = _prov_entry(concept_id, origin, release_id)
        elif lot == 2:
            data['second_line_therapy'] = drug_names
            data['second_line_therapy_id'] = concept_id
            data['second_line_component_ids'] = sorted(components)
            data['second_line_therapy_type_ids'] = sorted(classes)
            data['second_line_date'] = start_date
            data['second_line_start_date'] = start_date
            if end_date:
                data['second_line_end_date'] = end_date
            if concept_id:
                prov['second_line_therapy_id'] = _prov_entry(concept_id, origin, release_id)
        elif lot >= 3:
            later_components |= components
            if concept_id:
                later_origins.append(origin)
            later_classes |= classes
            later = data.get('later_therapies', [])
            # Keep the true episode line number, per-line concept_id (may be None
            # for an unresolved regimen) and origin so lines_of_therapy renders
            # the correct line number, pairs each later line with its own id, and
            # reports per-line asserted/inferred.
            later.append({'lineNumber': lot, 'therapy': drug_names,
                          'startDate': start_date, 'endDate': end_date, 'origin': origin,
                          'concept_id': concept_id})
            data['later_therapies'] = later
            if concept_id:
                ids = data.get('later_therapy_ids') or []
                ids.append(concept_id)
                data['later_therapy_ids'] = ids
            if not data.get('later_therapy'):
                data['later_therapy'] = drug_names
            if not data.get('later_date'):
                data['later_date'] = start_date
            if not data.get('later_start_date'):
                data['later_start_date'] = start_date
            if end_date and not data.get('later_end_date'):
                data['later_end_date'] = end_date

    if therapy_line_numbers:
        data['therapy_lines_count'] = len(therapy_line_numbers)
    if later_components:
        data['later_component_ids'] = sorted(later_components)
    if all_components:
        data['therapy_component_ids'] = sorted(all_components)
    if data.get('later_therapy_ids'):
        # One field-level origin for the aggregate list: 'asserted' only when
        # every resolved later line is asserted; per-line origins live in
        # later_therapies for the payload's per-line regimen_source.
        _later_origin = 'asserted' if later_origins and all(o == 'asserted' for o in later_origins) else 'inferred'
        prov['later_therapy_ids'] = {'value': data['later_therapy_ids'], 'origin': _later_origin, 'release_id': release_id}
    if prov:
        data['therapy_ids_provenance'] = prov
    if later_classes:
        data['later_therapy_type_ids'] = sorted(later_classes)
    if all_classes:
        data['therapy_type_ids'] = sorted(all_classes)

    # ── Per-line outcomes from LOT-N-outcome Observations ─────────────────
    # Written by the synthetic enrichment commands (and any future ingest path
    # that records a response assessment per line of therapy).
    response_code_map = {
        '182840001': 'Complete Response',
        '182841002': 'Partial Response',
        '182843004': 'Stable Disease',
        '182842009': 'Progressive Disease',
    }
    lot_outcomes: dict = {}
    # Use snapshot observations filtered in Python
    outcome_obs = sorted(
        (o for o in (snapshot.observations if snapshot else [])
         if (o.observation_source_value or '').startswith('LOT-')
         and (o.observation_source_value or '').endswith('-outcome')),
        key=lambda o: o.observation_date or date.min,
    )
    if not outcome_obs and not snapshot:
        # Legacy path: no snapshot provided
        outcome_obs = (
            Observation.objects
            .filter(
                person=person,
                is_erroneous=False,
                observation_source_value__startswith='LOT-',
                observation_source_value__endswith='-outcome',
            )
            .select_related('observation_concept')
            .order_by('observation_date')
        )
    for obs in outcome_obs:
        src = obs.observation_source_value or ''
        try:
            lot_n = int(src.split('-')[1])
        except (IndexError, ValueError):
            continue
        concept_code = obs.observation_concept.concept_code if obs.observation_concept else None
        outcome = obs.value_as_string or response_code_map.get(concept_code)
        if outcome:
            lot_outcomes[lot_n] = outcome  # ordered by date → latest assessment wins

    if 1 in lot_outcomes:
        data['first_line_outcome'] = lot_outcomes[1]
    if 2 in lot_outcomes:
        data['second_line_outcome'] = lot_outcomes[2]
    later_lots = sorted(k for k in lot_outcomes if k >= 3)
    if later_lots:
        data['later_outcome'] = lot_outcomes[later_lots[0]]

    _apply_treatment_assertions(data, person, episodes, snapshot)

    return data


def _treatment_assertion_value(row):
    """Read a typed treatment assertion without treating source codes as values."""
    value_concept = getattr(row, 'value_as_concept', None)
    return (
        _usable_concept_name(value_concept)
        or getattr(row, 'value_as_string', None)
        or getattr(row, 'value_source_value', None)
    )


def _apply_treatment_assertions(data, person, episodes, snapshot: OmopSnapshot = None):
    """Project dated intent/discontinuation assertions onto their Episode line.

    Explicit ``LOT-N-*`` source values win.  FHIR's standard LOINC therapy
    intent/discontinuation observations are persisted as Measurements; when
    they do not carry a line number, they are associated only when their event
    date falls within exactly one persisted Episode interval.  Ambiguous facts
    are deliberately left unprojected.
    """
    import re

    bounds = {
        e.episode_number: (e.episode_start_date, e.episode_end_date or e.episode_start_date)
        for e in episodes if e.episode_number and e.episode_start_date
    }
    if not bounds:
        return

    assertions = []  # (date, line, kind, value)
    pattern = re.compile(r'^LOT-(\d+)-(intent|discontinuation)$')
    # Snapshot observations are ordered -date -id (descending).  The dict
    # below uses last-write-wins, so iterate in ascending order so the
    # latest observation wins.
    obs_list = list(reversed(snapshot.observations)) if snapshot else list(
        Observation.objects.filter(person=person, is_erroneous=False)
        .select_related('value_as_concept').order_by('observation_date', 'observation_id')
    )
    for obs in obs_list:
        match = pattern.match(obs.observation_source_value or '')
        if not match:
            continue
        value = _treatment_assertion_value(obs)
        line = int(match.group(1))
        if value and line in bounds:
            assertions.append((obs.observation_date, line, match.group(2), value))

    # The FHIR import writes these non-laboratory observations to Measurement.
    # They are only assigned where the date has one unambiguous line owner.
    _INTENT_CODES = ('42804-5', '91379-3')
    if snapshot:
        intent_measurements = sorted(
            (m for code in _INTENT_CODES
             for m in snapshot.meas_by_code.get(code, []) + snapshot.meas_by_source.get(code, [])),
            key=lambda m: (m.measurement_date or date.min, m.measurement_id),
        )
        # Deduplicate (a row may appear in both code and source lists)
        seen_ids = set()
        deduped = []
        for m in intent_measurements:
            if m.measurement_id not in seen_ids:
                seen_ids.add(m.measurement_id)
                deduped.append(m)
        intent_measurements = deduped
    else:
        intent_measurements = list(
            Measurement.objects.filter(person=person, is_erroneous=False)
            .select_related('measurement_concept', 'value_as_concept')
            .filter(
                models.Q(measurement_concept__concept_code__in=_INTENT_CODES)
                | models.Q(measurement_source_value__in=_INTENT_CODES)
            ).order_by('measurement_date', 'measurement_id')
        )
    for measurement in intent_measurements:
        candidate_lines = [
            line for line, (start, end) in bounds.items()
            if start <= measurement.measurement_date <= end
        ]
        if len(candidate_lines) != 1:
            continue
        value = _treatment_assertion_value(measurement)
        if value:
            kind = 'intent' if _measurement_code(measurement) == '42804-5' else 'discontinuation'
            assertions.append((measurement.measurement_date, candidate_lines[0], kind, value))

    latest = {}
    for event_date, line, kind, value in assertions:
        latest[(line, kind)] = (event_date, value)

    field_prefix = {1: 'first_line', 2: 'second_line'}
    for (line, kind), (_, value) in latest.items():
        prefix = field_prefix.get(line, 'later' if line >= 3 else None)
        if prefix:
            field = f'{prefix}_{"intent" if kind == "intent" else "discontinuation_reason"}'
            data[field] = value[:50]

    # Legacy aggregate fields represent the most recently asserted line fact.
    for kind, aggregate in (
        ('intent', 'therapy_intent'),
        ('discontinuation', 'reason_for_discontinuation'),
    ):
        candidates = [entry for (line, entry_kind), entry in latest.items() if entry_kind == kind]
        if candidates:
            data[aggregate] = max(candidates, key=lambda entry: entry[0])[1][:100 if kind == 'discontinuation' else 50]

    # A washout is an observed gap between *persisted* therapy episodes, not a
    # guess from refresh time.  Preserve the minimum positive inter-line gap.
    ordered = sorted(bounds.items())
    gaps = [
        (next_start - current_end).days
        for (_, (_, current_end)), (_, (next_start, _)) in zip(ordered, ordered[1:])
        if next_start > current_end
    ]
    if gaps:
        data['washout_period_duration'] = f'{min(gaps)} days'
    data['line_of_therapy'] = str(max(bounds))


def _get_vitals_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    vital_sign_concepts = {
        'systolic_bp': '8480-6',
        'diastolic_bp': '8462-4',
        'heart_rate': '8867-4',
        'weight': '29463-7',
        'height': '8302-2',
        'temperature': '8310-5',
    }

    codes = set(vital_sign_concepts.values())
    # Filter from snapshot: measurements with value_as_number, matching codes
    measurements = [
        m for m in snapshot.measurements
        if m.value_as_number is not None
        and (getattr(m.measurement_concept, 'concept_code', None) in codes
             or m.measurement_source_value in codes)
    ]
    first_by_code = {}
    for measurement in measurements:
        code = _measurement_code(measurement)
        if code not in codes:
            # _measurement_code prefers whichever field looks like a LOINC; if
            # that is a code we did not ask for, fall back to the one that
            # actually matched so the row is not dropped.
            concept_code = getattr(measurement.measurement_concept, 'concept_code', None)
            code = concept_code if concept_code in codes else measurement.measurement_source_value
        first_by_code.setdefault(code, measurement)

    for vital_type, loinc_code in vital_sign_concepts.items():
        measurement = first_by_code.get(loinc_code)
        if not measurement:
            continue
        value = float(measurement.value_as_number)
        if vital_type == 'systolic_bp':
            data['systolic_blood_pressure'] = int(value)
        elif vital_type == 'diastolic_bp':
            data['diastolic_blood_pressure'] = int(value)
        elif vital_type == 'heart_rate':
            data['heartrate'] = int(value)
        elif vital_type == 'weight':
            data['weight'] = value
            data['weight_units'] = 'kg'
        elif vital_type == 'height':
            data['height'] = value
            data['height_units'] = 'cm'
        elif vital_type == 'temperature':
            data['temperature'] = value

    return data


_BIOMARKER_MEASUREMENT_LOINCS = frozenset({
    '83052-1',  # PD-L1 by clone 22C3 [Presence] in Tissue by Immune stain
    '16112-5',  # Estrogen receptor
    '16113-3',  # Progesterone receptor
    '48676-1',  # HER2
    '85319-2',  # Ki-67
    '83055-4',  # PD-L1 by clone 28-8 [Presence] in Tissue by Immune stain
    '83054-7',  # PD-L1 by clone 22C3 [Interpretation] in Tissue Narrative
    '44648-4',  # Biopsy/Nottingham grade
    # Menopausal status: no clean LOINC exists. 76690-7 is "Sexual orientation"
    # (Observation domain), not menopausal status. Removed to prevent
    # cross-contamination. Only 42802-9 "Age at menopause" is available, which
    # does not map to a status enum. Menopausal status will only populate from
    # the Observation table via _BIOMARKER_OBS_LOINCS (SNOMED/local codes).
})
# Menopausal status has no reliable LOINC; read from Observation only.
_BIOMARKER_OBS_LOINCS = frozenset({'44667-4'})
_HISTOLOGIC_TYPE_LOINCS = frozenset({'59847-4'})
_GENETIC_MUTATION_LOINCS = {
    '21636-6': 'BRCA1',
    '21640-8': 'BRCA2',   # BRCA2 gene c.6174delT [Presence] in Blood or Tissue
    '21739-8': 'TP53',    # TP53 gene mutations found [Identifier] in Blood or Tissue
    '48013-7': 'KRAS',
    '62862-8': 'EGFR',
    '60033-8': 'PIK3CA',  # PIK3CA gene mutations found [Identifier] in Blood or Tissue
}

# Pathology report fields which have an unambiguous LOINC representation in
# the FHIR payloads we accept.  The keys are source codes, not local concept
# ids: Athena reloads may change concept ids, while the source code is the
# durable vocabulary contract.
_GENOMICS_PATHOLOGY_LOINCS = frozenset({
    '85337-4',  # genomic/test methodology (also carries numeric Oncotype score)
    '31208-2',  # specimen source
    '69548-6',  # pathology test interpretation
    # Androgen receptor. 49457-5 ("Androgen receptor Ag [Presence] in Tissue by
    # Immune stain") is the only standard LOINC concept for this analyte and is
    # what new writes use. 82185-1 is not a LOINC code at all — it resolves
    # against no vocabulary release, so rows carrying it exist only as
    # source_value text. It stays readable so any deployment that already wrote
    # one still projects; nothing new is written under it.
    '49457-5',
    '82185-1',
    '92837-4',  # lymph-node involvement
    '21907-1',  # distant-metastasis status (not TNM M category)
    '44648-4',  # Nottingham biopsy grade
})

# MRD does not have one universal LOINC across oncology programmes.  We only
# accept an explicitly vocabulary-backed OMOP Genomic/NCIt/SNOMED assertion,
# never a free-text source value or a name from an arbitrary local vocabulary.
_MRD_VOCABULARIES = frozenset({'OMOP Genomic', 'NCIt', 'SNOMED'})


_LOINC_CODE_RE = __import__('re').compile(r'^\d+-\d+$')


def _measurement_code(measurement):
    """Return a stable measurement code from concept mapping or FHIR source_value.

    Prefers whichever of concept_code / measurement_source_value looks like a
    real LOINC code (pattern: digits-digits, e.g. '718-7').  This handles two
    common cases in imported data:
      - concept_code='16112-5', source_value='Estrogen receptor...' → concept_code
      - concept_code='0',       source_value='718-7'                → source_value
    Falls back to source_value if neither matches the LOINC pattern.
    """
    concept_code = getattr(measurement.measurement_concept, 'concept_code', None)
    source_value = measurement.measurement_source_value
    if concept_code and _LOINC_CODE_RE.match(concept_code):
        return concept_code
    if source_value and _LOINC_CODE_RE.match(source_value):
        return source_value
    return source_value or concept_code


def _observation_code(observation):
    """Return a stable observation code from concept mapping or FHIR source_value."""
    concept_code = getattr(observation.observation_concept, 'concept_code', None)
    source_value = observation.observation_source_value
    if concept_code and _LOINC_CODE_RE.match(concept_code):
        return concept_code
    if source_value and _LOINC_CODE_RE.match(source_value):
        return source_value
    return source_value or concept_code


def _get_biomarker_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    _all_biomarker_codes = _BIOMARKER_MEASUREMENT_LOINCS | _HISTOLOGIC_TYPE_LOINCS
    measurements = [
        m for m in snapshot.measurements
        if (getattr(m.measurement_concept, 'concept_code', None) in _all_biomarker_codes
            or m.measurement_source_value in _all_biomarker_codes)
    ]

    _all_obs_codes = _BIOMARKER_OBS_LOINCS | _HISTOLOGIC_TYPE_LOINCS
    observations = [
        o for o in snapshot.observations
        if (getattr(o.observation_concept, 'concept_code', None) in _all_obs_codes
            or o.observation_source_value in _all_obs_codes
            or (o.observation_concept and o.observation_concept.concept_name and any(
                term in o.observation_concept.concept_name.lower()
                for term in ('homologous recombination', 'bone only metastas', 'histologic')
            )))
    ]

    pdl1_test = next((m for m in measurements if _measurement_code(m) == '83052-1'), None)
    if pdl1_test:
        data['pd_l1_tumor_cells'] = int(pdl1_test.value_as_number) if pdl1_test.value_as_number else None
        data['pd_l1_assay'] = pdl1_test.value_source_value

    def _receptor_status(measurement):
        """Return a normalized receptor status from a Measurement row.

        Recognizes positive/negative/equivocal explicitly; any other non-empty
        value is preserved (upper-cased) rather than dropped, so clinically
        meaningful results such as a HER2 'Equivocal' (IHC 2+) reading are not
        silently lost. Returns None only when no value is present at all.
        """
        raw = None
        if measurement.value_as_concept:
            raw = measurement.value_as_concept.concept_name
        if not raw and measurement.value_as_string:
            raw = measurement.value_as_string
        if not raw and measurement.value_source_value:
            raw = measurement.value_source_value
        if not raw:
            return None
        s = raw.strip().lower()
        if 'positive' in s:
            return 'POSITIVE'
        if 'negative' in s:
            return 'NEGATIVE'
        if 'equivocal' in s:
            return 'EQUIVOCAL'
        return raw.strip().upper()

    er_measurement = next((m for m in measurements if _measurement_code(m) == '16112-5'), None)
    if er_measurement:
        status = _receptor_status(er_measurement)
        if status:
            data['estrogen_receptor_status'] = status

    pr_measurement = next((m for m in measurements if _measurement_code(m) == '16113-3'), None)
    if pr_measurement:
        status = _receptor_status(pr_measurement)
        if status:
            data['progesterone_receptor_status'] = status

    her2_measurement = next((m for m in measurements if _measurement_code(m) == '48676-1'), None)
    if her2_measurement:
        status = _receptor_status(her2_measurement)
        if status:
            data['her2_status'] = status

    if 'estrogen_receptor_status' in data and 'progesterone_receptor_status' in data and 'her2_status' in data:
        data['tnbc_status'] = (
            data['estrogen_receptor_status'] == 'NEGATIVE'
            and data['progesterone_receptor_status'] == 'NEGATIVE'
            and data['her2_status'] == 'NEGATIVE'
        )

    def _first_m(concept_code):
        """Return the most recent Measurement for a LOINC code, checking concept first then source_value."""
        return (
            next((m for m in measurements if _measurement_code(m) == concept_code), None)
            or next((m for m in measurements if m.measurement_source_value == concept_code), None)
        )

    # Ki-67 proliferation index — LOINC 85319-2
    ki67_m = _first_m('85319-2')
    if ki67_m and ki67_m.value_as_number is not None:
        data['ki67_proliferation_index'] = int(ki67_m.value_as_number)

    # PD-L1 immune cell percentage — LOINC 83055-4
    pdl1_ic_m = _first_m('83055-4')
    if pdl1_ic_m and pdl1_ic_m.value_as_number is not None:
        data['pd_l1_ic_percentage'] = int(pdl1_ic_m.value_as_number)

    # PD-L1 combined positive score — LOINC 83054-7
    pdl1_cps_m = _first_m('83054-7')
    if pdl1_cps_m and pdl1_cps_m.value_as_number is not None:
        data['pd_l1_combined_positive_score'] = int(pdl1_cps_m.value_as_number)

    # Biopsy (Nottingham) grade — LOINC 44648-4
    biopsy_m = _first_m('44648-4')
    if biopsy_m and biopsy_m.value_as_number is not None:
        data['biopsy_grade'] = int(biopsy_m.value_as_number)

    # Menopausal status — no reliable LOINC exists (76690-7 was "Sexual
    # orientation", not menopausal status). Read from Observation only, matched
    # by concept name containing 'menopaus'.
    menopause_obs = next(
        (obs for obs in observations
         if obs.observation_concept
         and obs.observation_concept.concept_name
         and 'menopaus' in obs.observation_concept.concept_name.lower()),
        None,
    )
    if menopause_obs:
        val = menopause_obs.value_as_string
        if not val and menopause_obs.value_as_concept:
            val = menopause_obs.value_as_concept.concept_name
        if val:
            data['menopausal_status'] = val

    # HRD status — Observation concept name containing 'homologous recombination'
    hrd_obs = next(
        (
            obs for obs in observations
            if obs.observation_concept
            and 'homologous recombination' in obs.observation_concept.concept_name.lower()
        ),
        None,
    )
    if hrd_obs:
        val = hrd_obs.value_as_string or hrd_obs.value_source_value
        if val:
            data['hrd_status'] = val

    # Bone-only metastasis status — LOINC 44667-4 or concept-name matching
    bone_obs = next((obs for obs in observations if _observation_code(obs) == '44667-4'), None)
    if not bone_obs:
        bone_obs = next(
            (
                obs for obs in observations
                if obs.observation_concept
                and 'bone only metastas' in obs.observation_concept.concept_name.lower()
            ),
            None,
        )
    if bone_obs:
        if bone_obs.value_as_number is not None:
            data['bone_only_metastasis_status'] = bool(int(bone_obs.value_as_number))
        elif bone_obs.value_as_string:
            s = bone_obs.value_as_string.lower()
            if s in ('yes', 'true', '1', 'positive'):
                data['bone_only_metastasis_status'] = True
            elif s in ('no', 'false', '0', 'negative'):
                data['bone_only_metastasis_status'] = False

    # Histologic type — Observation concept matching 'histologic' or 'histology'
    histologic_measurement = next(
        (
            m for m in measurements
            if _measurement_code(m) in _HISTOLOGIC_TYPE_LOINCS and m.value_as_string
        ),
        None,
    )
    if histologic_measurement and histologic_measurement.value_as_string:
        data['histologic_type'] = histologic_measurement.value_as_string
        return data

    histologic_obs = next(
        (
            obs for obs in observations
            if obs.value_as_string and (
                _observation_code(obs) in _HISTOLOGIC_TYPE_LOINCS
                or (
                    obs.observation_concept
                    and 'histolog' in obs.observation_concept.concept_name.lower()
                )
            )
        ),
        None,
    )
    if histologic_obs and histologic_obs.value_as_string:
        data['histologic_type'] = histologic_obs.value_as_string

    return data


def _coded_value(row):
    """Return the user-facing value carried by a coded OMOP fact.

    Prefer a value concept, then an explicit string/source value.  A concept-0
    value is intentionally suppressed by ``_usable_concept_name``.
    """
    return (
        _usable_concept_name(getattr(row, 'value_as_concept', None))
        or getattr(row, 'value_as_string', None)
        or getattr(row, 'value_source_value', None)
    )


def _get_genomics_pathology_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive report-level genomics/pathology fields from dated OMOP facts.

    This deliberately keys off standard vocabulary codes (or the explicit
    OMOP Genomic/NCIt/SNOMED MRD registry) rather than concept names.  It makes
    the projection portable across Athena loads and prevents a similarly named
    local concept from silently populating a clinical field.
    """
    snapshot = snapshot or _build_snapshot(person)
    data = {}
    measurements = [
        m for m in snapshot.measurements
        if (getattr(m.measurement_concept, 'concept_code', None) in _GENOMICS_PATHOLOGY_LOINCS
            or m.measurement_source_value in _GENOMICS_PATHOLOGY_LOINCS)
    ]

    def latest(code):
        return next((m for m in measurements if _measurement_code(m) == code), None)

    methodology = latest('85337-4')
    if methodology:
        value = _coded_value(methodology)
        if value:
            data['test_methodology'] = value[:50]
        if methodology.value_as_number is not None:
            data['oncotype_dx_score'] = int(methodology.value_as_number)

    specimen = latest('31208-2')
    if specimen:
        value = _coded_value(specimen)
        if value:
            data['test_specimen_type'] = value[:50]

    interpretation = latest('69548-6')
    if interpretation:
        value = _coded_value(interpretation)
        if value:
            data['report_interpretation'] = value[:50]

    report_facts = [row for row in (methodology, specimen, interpretation) if row]
    if report_facts:
        # Reports are not a separate CDM table.  Their safest compatible date
        # is therefore the newest dated report-level fact, never refresh time
        # or an undated projection patch.
        data['test_date'] = max(row.measurement_date for row in report_facts)

    # Prefer the real LOINC concept; fall back to the legacy non-code so an
    # existing row still projects. Order matters — a deployment holding both
    # should read the standard one.
    androgen_receptor = latest('49457-5') or latest('82185-1')
    if androgen_receptor:
        value = _coded_value(androgen_receptor)
        if value:
            data['androgen_receptor_status'] = value[:50]

    lymph_node = latest('92837-4')
    if lymph_node:
        value = _coded_value(lymph_node)
        if value:
            data['lymph_node_status'] = value[:50]

    metastasis = latest('21907-1')
    if metastasis:
        value = _coded_value(metastasis)
        if value:
            data['metastasis_status'] = value[:50]

    biopsy_grade = latest('44648-4')
    if biopsy_grade and biopsy_grade.value_as_number is not None:
        data['biopsy_grade_depr'] = str(int(biopsy_grade.value_as_number))

    mrd_observation = next(
        (
            obs for obs in snapshot.observations
            if obs.observation_concept
            and hasattr(obs.observation_concept, 'vocabulary') and obs.observation_concept.vocabulary
            and obs.observation_concept.vocabulary.vocabulary_id in _MRD_VOCABULARIES
            and obs.observation_concept.concept_name
            and 'minimal residual disease' in obs.observation_concept.concept_name.lower()
        ),
        None,
    )
    if mrd_observation:
        value = _coded_value(mrd_observation)
        if value:
            data['mrd_status'] = value[:50]

    mutations = _get_genetic_mutations(person, snapshot).get('genetic_mutations', [])
    if mutations:
        # ``genetic_mutations`` remains the structured canonical projection;
        # molecular_markers is its legacy display-compatible summary.
        data['molecular_markers'] = '; '.join(
            f"{mutation['gene'].upper()}: {mutation['variant']}"
            for mutation in mutations
        )

    return data


# 21908-9-riss is a non-standard code the MM generator uses for R-ISS stage
# (it mis-resolves to an unrelated concept on import, so it is matched by
# source_value only).
_STAGING_LOINCS = frozenset({'21908-9', '21908-9-riss', '21905-5', '21906-3', '21901-4'})


def _get_staging_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive TNM staging and overall stage group from OMOP Measurement/Observation rows.

    Reads staging data by LOINC code matched on either the concept code
    (OMOP-native path) or the source_value (FHIR upload path), across both
    Measurement and Observation rows.
    """
    snapshot = snapshot or _build_snapshot(person)
    data = {}

    measurements = [
        m for m in snapshot.measurements
        if (getattr(m.measurement_concept, 'concept_code', None) in _STAGING_LOINCS
            or m.measurement_source_value in _STAGING_LOINCS)
    ]
    _staging_obs_sources = _STAGING_LOINCS | {FHIR_CONDITION_STAGE_SOURCE_VALUE}
    observations = [
        o for o in snapshot.observations
        if (getattr(o.observation_concept, 'concept_code', None) in _STAGING_LOINCS
            or o.observation_source_value in _staging_obs_sources)
    ]

    def _stage_value(loinc_code):
        """Return the best string value for a staging LOINC code (Measurement then Observation)."""
        # Primary: concept code match
        m = next(
            (
                measurement for measurement in measurements
                if measurement.measurement_concept
                and measurement.measurement_concept.concept_code == loinc_code
            ),
            None,
        )
        if m:
            return m.value_as_string or (str(int(m.value_as_number)) if m.value_as_number is not None else None)
        # Secondary: LOINC stored as source_value (FHIR upload path)
        m = next((measurement for measurement in measurements if measurement.measurement_source_value == loinc_code), None)
        if m:
            return m.value_as_string or (str(int(m.value_as_number)) if m.value_as_number is not None else None)
        # Tertiary: Observation by concept code, then by source_value.
        obs = next(
            (
                observation for observation in observations
                if observation.observation_concept
                and observation.observation_concept.concept_code == loinc_code
            ),
            None,
        )
        if obs is None:
            obs = next(
                (observation for observation in observations
                 if observation.observation_source_value == loinc_code),
                None,
            )
        if obs:
            return obs.value_as_string or (
                str(int(obs.value_as_number)) if obs.value_as_number is not None else None
            )
        return None

    # Overall stage group. For MM both ISS (21908-9) and R-ISS (21908-9-riss)
    # are recorded; prefer R-ISS as it is the more current MM staging system.
    stage_val = _stage_value('21908-9-riss') or _stage_value('21908-9')
    if not stage_val:
        # Fallback for a dated FHIR Condition.stage assertion which had no
        # standard staging code. This is still an OMOP fact; its distinct
        # source value prevents it being confused with a coded LOINC result.
        stage_val = next(
            (
                observation.value_as_string
                for observation in observations
                if observation.observation_source_value == FHIR_CONDITION_STAGE_SOURCE_VALUE
                and observation.value_as_string
            ),
            None,
        )
    if stage_val:
        data['stage'] = stage_val

    # T stage — LOINC 21905-5
    t_val = _stage_value('21905-5')
    if t_val:
        data['tumor_stage'] = t_val

    # N stage — LOINC 21906-3
    n_val = _stage_value('21906-3')
    if n_val:
        data['nodes_stage'] = n_val

    # M (distant metastasis) stage — LOINC 21901-4
    m_val = _stage_value('21901-4')
    if m_val:
        data['distant_metastasis_stage'] = m_val

    # Staging modalities — Observation concept name containing 'staging method'
    staging_vals = list(dict.fromkeys(
        obs.value_as_string for obs in observations
        if obs.value_as_string
        and obs.observation_concept
        and 'staging' in obs.observation_concept.concept_name.lower()
    ))
    if staging_vals:
        data['staging_modalities'] = ', '.join(staging_vals)

    return data


def _get_bc_clinical_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive breast-cancer clinical assessment fields from OMOP Observation rows.

    Covers CTCAE toxicity/neuropathy grades stored as clinical assessments.
    """
    snapshot = snapshot or _build_snapshot(person)
    data = {}

    # Peripheral neuropathy CTCAE grade — concept name match
    pn_obs = next(
        (o for o in snapshot.observations
         if o.observation_concept and o.observation_concept.concept_name
         and 'peripheral neuropathy' in o.observation_concept.concept_name.lower()),
        None,
    )
    if pn_obs and pn_obs.value_as_number is not None:
        data['peripheral_neuropathy_grade'] = int(pn_obs.value_as_number)

    # Toxicity grade — CTCAE adverse event grade
    tox_obs = next(
        (o for o in snapshot.observations
         if o.observation_concept and o.observation_concept.concept_name
         and 'toxicity grade' in o.observation_concept.concept_name.lower()),
        None,
    )
    if not tox_obs:
        tox_obs = next(
            (o for o in snapshot.observations
             if o.observation_concept and o.observation_concept.concept_name
             and 'adverse event' in o.observation_concept.concept_name.lower()),
            None,
        )
    if tox_obs and tox_obs.value_as_number is not None:
        data['toxicity_grade'] = int(tox_obs.value_as_number)

    return data


def _get_social_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    _EMPLOYMENT_CODES = {'224362002', '160903007'}
    employment_obs = next(
        (o for o in snapshot.observations
         if getattr(o.observation_concept, 'concept_code', None) in _EMPLOYMENT_CODES),
        None,
    )
    if employment_obs:
        data['employment_status'] = employment_obs.value_as_string

    insurance_obs = next(
        (o for o in snapshot.observations
         if getattr(o.observation_concept, 'concept_code', None) == '408729009'),
        None,
    )
    if insurance_obs:
        data['insurance_type'] = insurance_obs.value_as_string

    return data


def _get_behavior_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    # New format (question/answer, #451): observation_concept = LOINC 72166-2,
    # answer in value_as_concept (LA18978-9 / LA15920-4 / LA18976-3).
    tobacco_qa_obs = [
        o for o in snapshot.observations
        if getattr(o.observation_concept, 'concept_code', None) == '72166-2'
        and getattr(o.observation_concept, 'vocabulary_id', None) == 'LOINC'
    ]
    _ANSWER_MAP = {
        'LA18978-9': (True,  'Never smoker'),
        'LA15920-4': (False, None),           # date appended below
        'LA18976-3': (False, 'Current smoker'),
    }
    for obs in tobacco_qa_obs:
        if obs.value_as_concept_id is None:
            continue
        answer_code = obs.value_as_concept.concept_code
        if answer_code in _ANSWER_MAP:
            no_use, details = _ANSWER_MAP[answer_code]
            data['no_tobacco_use_status'] = no_use
            if answer_code == 'LA15920-4':
                data['tobacco_use_details'] = f'Former smoker, quit {obs.observation_date}'
            else:
                data['tobacco_use_details'] = details

    # Backward compat: old format wrote SNOMED codes directly into
    # observation_concept_id. Only apply if the new format didn't match.
    if 'no_tobacco_use_status' not in data:
        _OLD_TOBACCO_CODES = {'266919005', '8517006', '77176002'}
        old_tobacco_obs = [
            o for o in snapshot.observations
            if getattr(o.observation_concept, 'concept_code', None) in _OLD_TOBACCO_CODES
        ]
        for obs in old_tobacco_obs:
            code = obs.observation_concept.concept_code
            if code == '266919005':
                data['no_tobacco_use_status'] = True
                data['tobacco_use_details'] = 'Never smoker'
            elif code == '8517006':
                data['no_tobacco_use_status'] = False
                data['tobacco_use_details'] = f'Former smoker, quit {obs.observation_date}'
            elif code == '77176002':
                data['no_tobacco_use_status'] = False
                data['tobacco_use_details'] = 'Current smoker'

    # snapshot.measurements is already ordered -date -id
    for measurement in snapshot.measurements:
        code = _measurement_code(measurement)
        field_info = _BEHAVIOR_MEASUREMENT_FIELDS.get(code)
        if not field_info:
            continue
        field_name, caster = field_info
        if field_name in data:
            continue

        if measurement.value_as_string not in (None, ''):
            if caster is str:
                data[field_name] = measurement.value_as_string
            else:
                try:
                    data[field_name] = caster(float(measurement.value_as_string))
                except (TypeError, ValueError):
                    continue
            continue

        if measurement.value_as_number is None:
            continue
        value = float(measurement.value_as_number)
        data[field_name] = caster(value) if caster is not str else str(value)

    return data


def _assertion_value(row, value_kind):
    """Return a typed assertion value, or None when the answer is unusable."""
    value = row.value_as_string
    if value in (None, '') and getattr(row, 'value_as_concept_id', None):
        value = _usable_concept_name(row.value_as_concept)
    if value in (None, ''):
        value = row.value_as_number
    if value is None:
        return None
    if value_kind == 'string':
        return str(value)

    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized not in {'true', 'false', 'yes', 'no', '1', '0'}:
            return None
        parsed = normalized in {'true', 'yes', '1'}
    else:
        # Numeric values originate from FHIR valueBoolean (1/0). Do not treat
        # arbitrary quantities such as 2 as a boolean clinical assertion.
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if numeric not in (0, 1):
            return None
        parsed = bool(numeric)
    return not parsed if value_kind == 'inverse_boolean' else parsed


def _get_assertion_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Project latest dated, typed OMOP eligibility/social assertions.

    Both OMOP tables are supported because FHIR ingestion uses Measurement for
    its coded Observations, while native OMOP integrations correctly use
    Observation for non-measurement facts.  OMOP requires a date and type
    concept for both tables; erroneous rows are excluded.  No row, or an
    unparseable answer, deliberately leaves the projection unknown.
    """
    snapshot = snapshot or _build_snapshot(person)
    measurement_rows = snapshot.measurements
    observation_rows = snapshot.observations
    rows = [
        (m.measurement_date, m.measurement_id, _measurement_code(m), m)
        for m in measurement_rows
    ] + [
        (o.observation_date, o.observation_id, _observation_code(o), o)
        for o in observation_rows
    ]
    rows.sort(key=lambda item: (item[0], item[1]), reverse=True)

    data = {}
    seen_fields = set()
    for _, _, code, row in rows:
        spec = _ASSERTION_FIELDS.get(code)
        if not spec:
            continue
        field, value_kind = spec
        if field in seen_fields:
            continue
        # A newer row with no valid typed answer supersedes an older answer:
        # falling back would resurrect a stale assertion after the source has
        # explicitly replaced it with an unknown/unusable value.
        seen_fields.add(field)
        value = _assertion_value(row, value_kind)
        if value is None:
            continue
        data[field] = value
        if field == 'pregnancy_test_result_value':
            # The event date comes from the same fact; it is never copied from
            # a separately patched PatientRecord column.
            data['pregnancy_test_date'] = row.measurement_date if isinstance(row, Measurement) else row.observation_date
    return data


def _get_infection_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    measurements = snapshot.measurements  # already non-erroneous

    def _infection_value(m):
        """Return 'negative', 'positive', or None from a Measurement row."""
        if m.value_as_concept_id:
            concept = _cc_by_id(m.value_as_concept_id)
            if concept:
                name = concept.concept_name.lower()
                if 'negative' in name:
                    return 'negative'
                if 'positive' in name:
                    return 'positive'
        if m.value_as_string:
            s = m.value_as_string.lower()
            if 'negative' in s or s in ('neg', 'n', 'non-reactive'):
                return 'negative'
            if 'positive' in s or s in ('pos', 'p', 'reactive', 'detected'):
                return 'positive'
        return None

    _HIV_CODES = {'5221-7', '7917-8'}
    hiv_measurements = [
        m for m in measurements
        if getattr(m.measurement_concept, 'concept_code', None) in _HIV_CODES
        or m.measurement_source_value in _HIV_CODES
    ]
    for m in hiv_measurements:
        result = _infection_value(m)
        if result == 'negative':
            data['no_hiv_status'] = True
            data['hiv_status'] = False
        elif result == 'positive':
            data['no_hiv_status'] = False
            data['hiv_status'] = True

    _HEPB_CODES = {'5195-3'}
    hepb_measurements = [
        m for m in measurements
        if getattr(m.measurement_concept, 'concept_code', None) in _HEPB_CODES
        or m.measurement_source_value in _HEPB_CODES
    ]
    for m in hepb_measurements:
        result = _infection_value(m)
        if result == 'negative':
            data['no_hepatitis_b_status'] = True
            data['hepatitis_b_status'] = False
        elif result == 'positive':
            data['no_hepatitis_b_status'] = False
            data['hepatitis_b_status'] = True

    _HEPC_CODES = {'5196-1'}
    hepc_measurements = [
        m for m in measurements
        if getattr(m.measurement_concept, 'concept_code', None) in _HEPC_CODES
        or m.measurement_source_value in _HEPC_CODES
    ]
    for m in hepc_measurements:
        result = _infection_value(m)
        if result == 'negative':
            data['no_hepatitis_c_status'] = True
            data['hepatitis_c_status'] = False
        elif result == 'positive':
            data['no_hepatitis_c_status'] = False
            data['hepatitis_c_status'] = True

    return data


#: IMWG 2014: involved/uninvolved ratio >= 100 with involved chain >= 100 mg/L.
_SLIM_FLC_RATIO = 100.0
_SLIM_INVOLVED_FLC_MG_L = 100.0


def _meets_flc_criterion(
    kappas: list[tuple[float, str | None]],
    lambdas: list[tuple[float, str | None]],
    ratios: list[float],
    person: Person,
) -> bool:
    """Whether the SLiM light chain criterion is established.

    Chains are (value, unit_source_value) in date order. Both halves must hold,
    and the absolute half needs mg/L, so a ratio with no chain result is
    unproven rather than satisfied.
    """
    if not kappas or not lambdas:
        return False

    k_raw, k_unit = kappas[-1]
    lam_raw, lam_unit = lambdas[-1]

    # A reported ratio beats one divided out of two results that may come from
    # different draws.
    ratio = None
    if ratios:
        ratio = ratios[-1]
    elif lam_raw > 0 and k_raw > 0:
        ratio = k_raw / lam_raw
    if ratio is None or ratio <= 0:
        return False

    if ratio >= _SLIM_FLC_RATIO:
        involved, involved_unit = k_raw, k_unit
    elif ratio <= 1 / _SLIM_FLC_RATIO:
        involved, involved_unit = lam_raw, lam_unit
    else:
        return False

    involved_mg_l = flc_to_canonical(involved, involved_unit)
    if involved_mg_l is None:
        logger.warning(
            'SLiM light-chain criterion left unproven for person_id=%s: involved '
            'free light chain has unit_source_value=%r, which is not convertible '
            'to mg/L', getattr(person, 'person_id', None), involved_unit,
        )
        return False
    return involved_mg_l >= _SLIM_INVOLVED_FLC_MG_L


def _get_mm_specific_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive MM-specific boolean and coded fields from OMOP Observation table.

    Covers fields not derivable via _LOINC_LAB_FIELDS (boolean / string-valued
    observations): plasma_cell_leukemia, bone_lesions, meets_crab.
    Uses measurement_source_value as the LOINC lookup since Concept rows may not
    be loaded in all environments.
    """
    snapshot = snapshot or _build_snapshot(person)
    data = {}

    # Query Measurement first (upload handler routes all FHIR Obs through Measurement
    # when they have a recognisable LOINC code).
    MM_LOINC_CODES = {
        '47082-2': 'plasma_cell_leukemia',   # Plasma cell leukemia status (boolean)
        '24646-7': 'bone_lesions',            # Bone lesion presence (boolean → text)
        '89599-5': 'meets_crab',              # CRAB criteria met (boolean)
    }

    mm_measurements = sorted(
        (m for m in snapshot.measurements
         if m.measurement_source_value in MM_LOINC_CODES),
        key=lambda m: m.measurement_date or date.min,
    )
    def _coerce_mm_boolean(number_value, string_value):
        if number_value is not None:
            return bool(number_value)
        if string_value:
            return string_value.lower() in ('true', '1', 'yes', 'present')
        return None

    def _set_mm_field(field, bool_val):
        if bool_val is None or field in data:
            return
        if field == 'bone_lesions':
            data[field] = 'Present' if bool_val else 'Absent'
        else:
            data[field] = bool_val

    for m in mm_measurements:
        field = MM_LOINC_CODES.get(m.measurement_source_value)
        _set_mm_field(field, _coerce_mm_boolean(m.value_as_number, m.value_as_string))

    # Observation rows should supplement missing fields, not only act as an
    # all-or-nothing fallback, because some environments split MM facts across
    # Measurement and Observation tables.
    mm_obs = sorted(
        (o for o in snapshot.observations
         if o.observation_source_value in MM_LOINC_CODES),
        key=lambda o: o.observation_date or date.min,
    )
    for o in mm_obs:
        field = MM_LOINC_CODES.get(o.observation_source_value)
        _set_mm_field(field, _coerce_mm_boolean(o.value_as_number, o.value_as_string))

    # ── meets_slim: derived from plasma cells and FLC ratio in OMOP ─────────
    # SLiM = Sixty (plasma cells ≥60%), Light chain ratio ≥100, or MRI lesions
    # (MRI lesions are not tracked in OMOP, so we check plasma cells and FLC)
    # EHR rows carry the display name in source_value and the LOINC on the
    # concept, so a source_value-only filter saw demo data and nothing else.
    slim_vals = {}
    slim_measurements = sorted(
        (m for m in snapshot.measurements
         if m.value_as_number is not None
         and (getattr(m.measurement_concept, 'concept_code', None) in _SLIM_MATCH_VALUES
              or m.measurement_source_value in _SLIM_MATCH_VALUES)),
        key=lambda m: (m.measurement_date or date.min, m.measurement_id),
    )
    for m in slim_measurements:
        code = _measurement_code(m)
        field = _LOINC_LAB_FIELDS.get(code, (None, None))[0]
        if field is None:
            # The row matched on source_value, so its concept resolved to some
            # other code. Fall back to the display name map.
            field = _SOURCE_VALUE_LAB_FIELDS.get(m.measurement_source_value)
        if field in _SLIM_FIELDS:
            slim_vals.setdefault(field, []).append(
                (float(m.value_as_number), m.unit_source_value))

    plasma_pcts = [v for v, _unit in slim_vals.get('clonal_plasma_cells', [])]
    kappas = slim_vals.get('kappa_flc', [])
    lambdas = slim_vals.get('lambda_flc', [])
    ratios = [v for v, _unit in slim_vals.get('free_light_chain_ratio', [])]

    meets_slim = False
    # Sixty criterion: any plasma cells measurement ≥60%
    if any(v >= 60.0 for v in plasma_pcts):
        meets_slim = True
    # Ratio alone is not enough. A patient with both chains low reaches a ratio
    # of 100 without the absolute burden, and this decides a myeloma defining
    # event.
    elif _meets_flc_criterion(kappas, lambdas, ratios, person):
        meets_slim = True

    data['meets_slim'] = meets_slim

    # ── meets_crab fallback: compute from OMOP Measurements when obs missing ─
    if 'meets_crab' not in data:
        _CRAB_CODES = {'718-7', '59260-0', '17861-6', '2000-0', '2164-2', '33914-3'}
        crab_vals = {}
        for m in snapshot.measurements:
            if m.measurement_source_value in _CRAB_CODES and m.value_as_number is not None:
                crab_vals.setdefault(m.measurement_source_value, []).append(float(m.value_as_number))

        hgb_vals = crab_vals.get('718-7', []) or crab_vals.get('59260-0', [])
        ca_vals = crab_vals.get('17861-6', []) or crab_vals.get('2000-0', [])
        crcl_vals = crab_vals.get('2164-2', []) or crab_vals.get('33914-3', [])

        crab_anemia = hgb_vals and min(hgb_vals) < 10.0
        crab_calcium = ca_vals and max(ca_vals) > 11.0
        crab_renal = crcl_vals and min(crcl_vals) < 40.0
        crab_bone = data.get('bone_lesions') == 'Present'

        if any([crab_anemia, crab_calcium, crab_renal, crab_bone]):
            data['meets_crab'] = True

    return data


def _get_sct_cytogenetic_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive cytogenetic markers and SCT fields from OMOP Observation rows.

    These values are written as Observation rows by the bulk FHIR import
    (and eventually the upload handler) with custom observation_source_value
    keys: mm-cytogenetic-markers, mm-sct-date, mm-sct-history, mm-sct-eligibility.
    """
    snapshot = snapshot or _build_snapshot(person)
    data = {}
    _SOURCE_KEYS = frozenset({
        'mm-cytogenetic-markers', 'mm-sct-date',
        'mm-sct-history', 'mm-sct-eligibility',
    })
    # snapshot.observations is already ordered -observation_date
    obs_qs = [
        o for o in snapshot.observations
        if o.observation_source_value in _SOURCE_KEYS
    ]
    seen = set()
    for obs in obs_qs:
        src = obs.observation_source_value
        if src in seen:
            continue  # take most recent only
        seen.add(src)
        val = obs.value_as_string
        if not val:
            continue

        if src == 'mm-cytogenetic-markers':
            data['cytogenic_markers'] = val
        elif src == 'mm-sct-date':
            try:
                data['sct_date'] = datetime.strptime(val[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                pass
        elif src == 'mm-sct-history':
            data['stem_cell_transplant_history'] = [
                t.strip() for t in val.split(',') if t.strip()
            ]
        elif src == 'mm-sct-eligibility':
            data['sct_eligibility'] = [
                t.strip() for t in val.split(',') if t.strip()
            ]

    return data


def _get_assessment_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    # snapshot.observations is already ordered -observation_date, non-erroneous
    tumor_stage_obs = next(
        (o for o in snapshot.observations
         if getattr(o.observation_concept, 'concept_code', None) == '21905-5'),
        None,
    )
    metastasis_obs = next(
        (o for o in snapshot.observations
         if getattr(o.observation_concept, 'concept_code', None) == '21901-4'),
        None,
    )

    t_stage_val = tumor_stage_obs.value_as_string if tumor_stage_obs else None
    m_stage_val = metastasis_obs.value_as_string if metastasis_obs else None

    if t_stage_val or m_stage_val:
        if (m_stage_val and 'M1' in m_stage_val) or (
            t_stage_val and any(t_stage_val.startswith(t) for t in ['T3', 'T4'])
        ):
            data['measurable_disease_by_recist_status'] = True
        elif m_stage_val and 'Unknown' not in m_stage_val:
            data['measurable_disease_by_recist_status'] = False

    return data


def _get_laboratory_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    # snapshot.measurements already ordered -date -id, non-erroneous, select_related
    measurements = snapshot.measurements

    # --- Legacy fields via exact historic concept-name matching ---
    for measurement in measurements:
        if not measurement.measurement_concept or measurement.value_as_number is None:
            continue
        concept_name = measurement.measurement_concept.concept_name.casefold().strip()
        field_name = _LEGACY_LAB_CONCEPT_FIELDS.get(concept_name)
        if not field_name or field_name in data:
            continue
        data[field_name] = measurement.value_as_number
        if measurement.unit_source_value:
            data[f'{field_name}_units'] = measurement.unit_source_value

    # --- New UI fields via LOINC concept code (primary path) ---
    loinc_ms = [m for m in measurements if m.value_as_number is not None]
    wbc_projection_blocked = False
    for m in loinc_ms:
        code = _measurement_code(m)
        if code not in _LOINC_LAB_FIELDS:
            continue
        field, cast = _LOINC_LAB_FIELDS[code]
        canonical_value = None
        if code == '6690-2':
            if wbc_projection_blocked:
                continue
            canonical_value = wbc_to_canonical(m.value_as_number, m.unit_source_value)
            if canonical_value is None:
                # This is the latest WBC row due to the query ordering. Do not
                # silently substitute an older result with a known unit.
                wbc_projection_blocked = True
                logger.warning(
                    'WBC projections skipped for person_id=%s: unsupported or absent '
                    'unit_source_value=%r', person.person_id, m.unit_source_value,
                )
                continue
        if field in _FLC_FIELDS:
            canonical_flc = flc_to_canonical(m.value_as_number, m.unit_source_value)
            if canonical_flc is None:
                # Unlike WBC this still projects: blanking thousands of patients
                # is worse than the lab's own unit. The SLiM threshold refuses
                # it instead, which is where a 10x error changes the answer.
                logger.warning(
                    'FLC projected without unit conversion for person_id=%s: '
                    'unit_source_value=%r is not convertible to mg/L',
                    person.person_id, m.unit_source_value,
                )
            else:
                if field not in data:
                    data[field] = cast(canonical_flc)
                continue
        if field not in data:
            if code != '6690-2' or canonical_value is not None:
                data[field] = cast(canonical_value if code == '6690-2' else m.value_as_number)
        if code == '6690-2' and canonical_value is not None and 'white_blood_cell_count' not in data:
            unit_system = (
                PatientRecord.objects.filter(person=person)
                .values_list('organization__clinical_unit_system', flat=True)
                .first()
            )
            data['white_blood_cell_count'] = canonical_value
            data['white_blood_cell_count_units'] = canonical_wbc_unit(unit_system)

    # --- New UI fields via display-name source_value (legacy/generator path) ---
    unfound = {f for (f, _) in _LOINC_LAB_FIELDS.values() if f not in data}
    if unfound:
        sv_ms = [
            m for m in measurements
            if m.measurement_source_value in _SOURCE_VALUE_LAB_FIELDS
            and m.value_as_number is not None
        ]
        for m in sv_ms:
            field = _SOURCE_VALUE_LAB_FIELDS.get(m.measurement_source_value)
            if field == 'wbc_count_thousand_per_ul' and wbc_projection_blocked:
                continue
            if field and field not in data:
                data[field] = float(m.value_as_number)

    # --- Copy canonical values to legacy aliases (issue #471) ---
    for canonical, aliases in _LAB_FIELD_ALIASES.items():
        value = data.get(canonical)
        if value is not None:
            for alias in aliases:
                data[alias] = value

    return data


# Performance status lands in either OMOP table depending on how it arrived:
# the FHIR upload can route these LOINCs to `observation`, while other OMOP
# imports may use `measurement`. Reading only one table loses valid OMOP facts.
_ECOG_LOINC = '89247-1'
_KARNOFSKY_LOINC = '89243-0'


def _performance_rows(snapshot: OmopSnapshot, name_fragment: str, loinc_code: str) -> list:
    """Latest-first performance scores from both `observation` and `measurement`.

    Matches on concept name or LOINC code so a row still resolves when the
    vocabulary is not loaded and the code survives only in the source value.
    """
    name_lower = name_fragment.lower()
    obs = [
        (o.observation_date, o.value_as_number)
        for o in snapshot.observations
        if o.value_as_number is not None
        and (
            (o.observation_concept and o.observation_concept.concept_name
             and name_lower in o.observation_concept.concept_name.lower())
            or getattr(o.observation_concept, 'concept_code', None) == loinc_code
            or o.observation_source_value == loinc_code
        )
    ]
    meas = [
        (m.measurement_date, m.value_as_number)
        for m in snapshot.measurements
        if m.value_as_number is not None
        and (
            (m.measurement_concept and m.measurement_concept.concept_name
             and name_lower in m.measurement_concept.concept_name.lower())
            or getattr(m.measurement_concept, 'concept_code', None) == loinc_code
            or m.measurement_source_value == loinc_code
        )
    ]
    # Measurements first, and the sort below is stable (reverse=True preserves
    # the order of equal keys), so a same-date tie resolves in favour of
    # `measurement`. That is the PATCH write-through's table and
    # _sync_measurement always stamps today, so the tie case is "clinician
    # corrected a score a bundle loaded the same day" — the correction has to
    # win, or the refresh silently reverts it.
    rows = meas + obs
    # date.min for undated rows so a dated score always outranks one with no date.
    rows.sort(key=lambda r: (r[0] or date.min), reverse=True)
    return rows


def _get_performance_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    ecog = _performance_rows(snapshot, 'ecog', _ECOG_LOINC)
    if ecog:
        ecog_date, ecog_value = ecog[0]
        data['ecog_performance_status'] = int(ecog_value)
        if ecog_date:
            data['ecog_assessment_date'] = ecog_date

    karnofsky = _performance_rows(snapshot, 'karnofsky', _KARNOFSKY_LOINC)
    if karnofsky:
        data['karnofsky_performance_score'] = int(karnofsky[0][1])

    return data


def _get_genetic_mutations(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)

    origin_concepts = {255395001: 'germline', 255461003: 'somatic'}
    interpretation_concepts = {30166007: 'pathogenic', 10828004: 'benign', 42425007: 'vus'}

    mutations = []

    _gen_codes = set(_GENETIC_MUTATION_LOINCS.keys())
    genetic_measurements = [
        m for m in snapshot.measurements
        if (getattr(m.measurement_concept, 'concept_code', None) in _gen_codes
            or m.measurement_source_value in _gen_codes)
    ]

    for measurement in genetic_measurements:
        if not measurement.value_as_string:
            continue
        gene = _GENETIC_MUTATION_LOINCS.get(_measurement_code(measurement))
        if not gene:
            continue

        mutation_data = {
            'gene': gene.lower(),
            'variant': measurement.value_as_string,
            'test_date': measurement.measurement_date.isoformat() if measurement.measurement_date else None,
        }

        if measurement.qualifier_concept and measurement.qualifier_concept.concept_id in origin_concepts:
            mutation_data['origin'] = origin_concepts[measurement.qualifier_concept.concept_id]

        if measurement.value_as_concept and measurement.value_as_concept.concept_id in interpretation_concepts:
            mutation_data['interpretation'] = interpretation_concepts[measurement.value_as_concept.concept_id]

        if measurement.qualifier_source_value:
            mutation_data['assay_method'] = measurement.qualifier_source_value

        mutations.append(mutation_data)

    data['genetic_mutations'] = mutations
    return data


def _get_tumor_size_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive tumour size from the semantically tumour-specific LOINC row.

    LOINC 21889-1 is ``Size Tumor``.  Some legacy feeds reused it for a
    lymph-node measurement; those rows must carry an explicit
    ``qualifier_source_value=lymph-node`` and are routed by ``_get_cll_data``.
    A code-only row is therefore never allowed to populate both projections.
    """
    snapshot = snapshot or _build_snapshot(person)
    row = next(
        (m for m in snapshot.measurements
         if m.value_as_number is not None
         and (getattr(m.measurement_concept, 'concept_code', None) == '21889-1'
              or m.measurement_source_value == '21889-1')
         and (m.qualifier_source_value or '').lower() != 'lymph-node'),
        None,
    )
    return {'tumor_size': float(row.value_as_number)} if row else {}


def _get_cll_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)
    measurements = snapshot.measurements
    observations = snapshot.observations
    conditions = snapshot.conditions

    # 731-0 (ALC) is intentionally absent — the canonical column is
    # alc_thousand_per_ul, populated by _get_laboratory_data via
    # _LOINC_LAB_FIELDS.  Deriving absolute_lymphocyte_count here duplicated
    # the value under an ambiguous name (see issue #544).
    loinc_map = {
        '48094-6': 'serum_beta2_microglobulin_level',
        '8632-1':  'qtcf_value',
        '44996-6': 'spleen_size',
    }
    for loinc_code, field in loinc_map.items():
        m = next(
            (m for m in measurements
             if getattr(m.measurement_concept, 'concept_code', None) == loinc_code
             and m.value_as_number is not None),
            None,
        )
        if m:
            data[field] = float(m.value_as_number)

    # A lymph-node size is a distinct clinical meaning from LOINC 21889-1
    # (Size Tumor).  Require the explicit source qualifier so the same row can
    # never populate both PatientRecord columns.
    lymph_node = next(
        (m for m in measurements
         if (getattr(m.measurement_concept, 'concept_code', None) == '21889-1'
             or m.measurement_source_value == '21889-1')
         and (m.qualifier_source_value or '').lower() == 'lymph-node'
         and m.value_as_number is not None),
        None,
    )
    if lymph_node:
        data['largest_lymph_node_size'] = float(lymph_node.value_as_number)

    for m in measurements:
        if not m.measurement_concept:
            continue
        cname = m.measurement_concept.concept_name.lower()
        if 'clonal' in cname and 'bone marrow' in cname and 'b' in cname and m.value_as_number:
            data['clonal_bone_marrow_b_lymphocytes'] = float(m.value_as_number)
        elif 'clonal b' in cname and 'lymphocyte' in cname and m.value_as_number:
            data['clonal_b_lymphocyte_count'] = int(m.value_as_number)

    cd_markers = []
    for m in measurements:
        cname = m.measurement_concept.concept_name if m.measurement_concept else ''
        if any(cd in cname.upper() for cd in ('CD38', 'CD20', 'CD5', 'ZAP70')):
            if m.value_as_string:
                cd_markers.append(f'{cname}: {m.value_as_string}')
    if cd_markers:
        data['protein_expressions'] = ', '.join(cd_markers)

    for obs in observations:
        if not obs.observation_concept:
            continue
        cname = obs.observation_concept.concept_name.lower()
        val_str = (obs.value_as_string or '').lower()
        val_num = obs.value_as_number

        if 'binet' in cname:
            stage_val = obs.value_as_string or (str(int(val_num)) if val_num else None)
            if stage_val:
                data['binet_stage'] = stage_val
        elif 'tumor burden' in cname or 'tumour burden' in cname:
            data['tumor_burden'] = obs.value_as_string or cname
        elif 'disease activity' in cname:
            data['disease_activity'] = obs.value_as_string or cname
        elif 'bone marrow involvement' in cname or 'bone marrow infiltrat' in cname:
            data['bone_marrow_involvement'] = val_str in ('true', 'yes', '1') or val_num == 1
        elif 'hepatomegaly' in cname:
            data['hepatomegaly'] = val_str in ('true', 'yes', '1', 'present') or val_num == 1
        elif 'splenomegaly' in cname:
            data['splenomegaly'] = val_str in ('true', 'yes', '1', 'present') or val_num == 1
        elif 'lymphadenopathy' in cname:
            data['lymphadenopathy'] = val_str in ('true', 'yes', '1', 'present') or val_num == 1
        elif 'autoimmune cytopenia' in cname:
            data['autoimmune_cytopenias_refractory_to_steroids'] = (
                val_str in ('true', 'yes', '1', 'refractory') or val_num == 1
            )

    for cond in conditions:
        if not cond.condition_concept:
            continue
        cname = (cond.condition_concept.concept_name or '').lower()
        if 'richter' in cname:
            data['richter_transformation'] = cond.condition_concept.concept_name
        if 'hepatomegaly' in cname and 'hepatomegaly' not in data:
            data['hepatomegaly'] = True
        if 'splenomegaly' in cname and 'splenomegaly' not in data:
            data['splenomegaly'] = True
        if 'lymphadenopathy' in cname and 'lymphadenopathy' not in data:
            data['lymphadenopathy'] = True

    drug_exposures = snapshot.drug_exposures
    btk_terms = ('ibrutinib', 'zanubrutinib', 'acalabrutinib', 'pirtobrutinib')
    bcl2_terms = ('venetoclax',)

    had_btk = any(
        any(t in (de.drug_concept.concept_name or '').lower() for t in btk_terms)
        for de in drug_exposures if de.drug_concept
    )
    had_bcl2 = any(
        any(t in (de.drug_concept.concept_name or '').lower() for t in bcl2_terms)
        for de in drug_exposures if de.drug_concept
    )

    has_progression = any(
        getattr(o.observation_concept, 'concept_code', None) == '182842009'
        for o in observations
    )

    if had_btk:
        data['btk_inhibitor_refractory'] = has_progression
    if had_bcl2:
        data['bcl2_inhibitor_refractory'] = has_progression

    # ALC doubling time — filter from snapshot by LOINC concept code 731-0
    alc_rows = sorted(
        (m for m in measurements
         if getattr(m.measurement_concept, 'concept_code', None) == '731-0'
         and getattr(getattr(m.measurement_concept, 'vocabulary', None), 'vocabulary_id', None) == 'LOINC'
         and m.value_as_number is not None),
        key=lambda m: m.measurement_date or date.min,
    )
    if len(alc_rows) >= 2:
        pts = [(m.measurement_date, m.value_as_number) for m in alc_rows]
        ldt = _compute_lymphocyte_doubling_time(pts)
        if ldt is not None:
            data['lymphocyte_doubling_time'] = ldt

    return data


def _get_lymphoma_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    data = {}
    snapshot = snapshot or _build_snapshot(person)
    observations = snapshot.observations
    measurements = snapshot.measurements

    for obs in observations:
        if not obs.observation_concept:
            continue
        cname = obs.observation_concept.concept_name.lower()
        if 'flipi' in cname:
            if obs.value_as_number is not None:
                data['flipi_score'] = int(obs.value_as_number)
            elif obs.value_as_string:
                data['flipi_score_options'] = obs.value_as_string
        elif 'gelf' in cname:
            data['gelf_criteria_status'] = obs.value_as_string or cname

    for m in measurements:
        if not m.measurement_concept:
            continue
        cname = m.measurement_concept.concept_name.lower()
        if 'grade' in cname and m.value_as_number is not None:
            data['tumor_grade'] = int(m.value_as_number)

    data.update(_get_dlbcl_transformation(person, observations, snapshot))

    return data


# ---------------------------------------------------------------------------
# FL → DLBCL histologic transformation
# ---------------------------------------------------------------------------

_DLBCL_CONDITION_TERMS = ('diffuse large b-cell', 'dlbcl')

# Maps LOT-line outcome strings onto PostTransformationOutcome titles.
# Ordered: 'stringent complete response' contains 'complete response', and
# 'very good partial response' contains 'partial response', so substring
# matching in this order lands on the right bucket.
_TRANSFORMATION_OUTCOME_MAP = (
    ('complete response', 'Complete Response'),
    ('partial response', 'Partial Response'),
    ('stable disease', 'Stable Disease'),
    ('progressive', 'Progressive Disease'),
    ('relapse', 'Progressive Disease'),
)


def _normalize_transformation_outcome(outcome: str) -> str:
    low = (outcome or '').lower()
    for term, title in _TRANSFORMATION_OUTCOME_MAP:
        if term in low:
            return title
    return 'Unknown'


def _get_dlbcl_transformation(person: Person, observations, snapshot: OmopSnapshot = None) -> dict:
    """Derive FL → DLBCL transformation fields from OMOP.

    Evidence, in order:
      1. a DLBCL ConditionOccurrence (transformation date = its start date)
      2. fallback: an Observation whose concept name or source value mentions
         histologic transformation (transformation date = observation date)

    Post-transformation outcome precedence: death on/after transformation →
    'Deceased'; else the latest post-transformation LOT-line outcome
    Observation; else None (manual entry).

    `observations` is the person's Observation list ordered by
    -observation_date (already fetched by _get_lymphoma_data).
    """
    snapshot = snapshot or _build_snapshot(person)
    data = {}

    transformation_date = None
    # snapshot.conditions is -start_date ordered; we need earliest DLBCL, so sort ascending
    dlbcl_conditions = sorted(snapshot.conditions, key=lambda c: c.condition_start_date or date.min)
    for cond in dlbcl_conditions:
        cname = (cond.condition_concept.concept_name or '').lower() if cond.condition_concept else ''
        if any(term in cname for term in _DLBCL_CONDITION_TERMS):
            transformation_date = cond.condition_start_date
            break

    if transformation_date is None:
        for obs in observations:
            cname = (obs.observation_concept.concept_name or '').lower() if obs.observation_concept else ''
            src = (obs.observation_source_value or '').lower()
            if 'transformation' in cname or 'transformed' in src:
                transformation_date = obs.observation_date
                break

    if transformation_date is None:
        data['transformed_to_dlbcl'] = False
        return data

    data['transformed_to_dlbcl'] = True
    data['dlbcl_transformation_date'] = transformation_date

    death = snapshot.death
    if death and death.death_date and death.death_date >= transformation_date:
        data['post_transformation_outcome'] = 'Deceased'
    else:
        for obs in observations:  # -observation_date: first hit is latest
            src = obs.observation_source_value or ''
            if (src.startswith('LOT-') and src.endswith('-outcome')
                    and obs.observation_date >= transformation_date
                    and obs.value_as_string):
                data['post_transformation_outcome'] = _normalize_transformation_outcome(obs.value_as_string)
                break

    return data


def _get_prior_procedures(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Extract ProcedureOccurrence records into prior_procedures JSONField."""
    snapshot = snapshot or _build_snapshot(person)
    data = {}

    procedures = snapshot.procedures  # already ordered -procedure_date

    if procedures:
        procedure_list = []
        for proc in procedures:
            procedure_list.append({
                'procedure': proc.procedure_concept.concept_name if proc.procedure_concept else 'Unknown',
                'date': str(proc.procedure_date) if proc.procedure_date else None,
                'concept_id': proc.procedure_concept_id,
            })
        data['prior_procedures'] = procedure_list

    return data


# ---------------------------------------------------------------------------
# Derived fields (must run after all sections are populated)
# ---------------------------------------------------------------------------

def _parse_date_value(v):
    """Parse a date value that may be a date object or an ISO date string."""
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v)
        except ValueError:
            return None
    return None


def _compute_derived_fields(patient_info: PatientRecord) -> None:
    """Compute fields that depend on other PatientRecord fields being set."""
    serum_mp = patient_info.monoclonal_protein_serum
    urine_mp = patient_info.monoclonal_protein_urine
    kappa = patient_info.kappa_flc
    lam = patient_info.lambda_flc

    imwg = None
    if serum_mp is not None and float(serum_mp) >= 0.5:
        imwg = True
    elif urine_mp is not None and float(urine_mp) >= 200:
        imwg = True
    elif kappa is not None and lam is not None and lam > 0:
        ratio = kappa / lam
        diff = abs(kappa - lam)
        if (ratio > 100 or ratio < 0.01) and diff >= 10:
            imwg = True
    elif any(v is not None for v in (serum_mp, urine_mp, kappa, lam)):
        imwg = False
    patient_info.measurable_disease_imwg = imwg

    # Use the canonical LOINC-derived column (10³/µL) for the iwCLL 5.0
    # threshold.  absolute_lymphocyte_count is no longer derived (issue #544).
    alc = patient_info.alc_thousand_per_ul
    lns = patient_info.largest_lymph_node_size
    spleen = patient_info.splenomegaly
    liver = patient_info.hepatomegaly

    has_any_iwcll_data = any(v is not None for v in (alc, lns, spleen, liver))
    if has_any_iwcll_data:
        patient_info.measurable_disease_iwcll = bool(
            (alc is not None and float(alc) >= 5.0)
            or (lns is not None and float(lns) >= 1.5)
            or spleen is True
            or liver is True
        )
    else:
        patient_info.measurable_disease_iwcll = None

    mutations = patient_info.genetic_mutations or []
    patient_info.tp53_disruption = any(
        m.get('gene', '').lower() == 'tp53' and m.get('interpretation') == 'pathogenic'
        for m in mutations
    )

    # BMI — computed from weight and height when units are known
    weight = patient_info.weight
    height = patient_info.height
    if weight is not None and height is not None and float(height) > 0:
        weight_units = (patient_info.weight_units or 'kg').lower()
        height_units = (patient_info.height_units or 'cm').lower()
        weight_kg = float(weight) * (0.453592 if weight_units in ('lbs', 'lb') else 1.0)
        height_m = float(height) * (0.0254 if height_units in ('in', 'inch', 'inches') else 0.01)
        if height_m >= 0.5:  # sanity-check: skip implausible heights
            patient_info.bmi = round(weight_kg / (height_m ** 2), 1)

    # HR status — derived from ER and PR receptor status (HR+ = ER+ or PR+)
    er = patient_info.estrogen_receptor_status
    pr = patient_info.progesterone_receptor_status
    if er is not None or pr is not None:
        if (er and 'positive' in er.lower()) or (pr and 'positive' in pr.lower()):
            patient_info.hr_status = 'HR+'
        elif er == 'NEGATIVE' and pr == 'NEGATIVE':
            patient_info.hr_status = 'HR-'

    # Metastatic status — True when M stage is M1
    m_stage = patient_info.distant_metastasis_stage
    if m_stage is not None:
        patient_info.metastatic_status = 'M1' in m_stage.upper()

    # Renal adequacy — eGFR >= 30 mL/min/1.73m² (CTCAE G4 threshold)
    egfr = patient_info.egfr_ml_min_173m2
    creatinine = patient_info.serum_creatinine_mg_dl
    if egfr is not None:
        patient_info.renal_adequacy_status = float(egfr) >= 30.0
    elif creatinine is not None:
        patient_info.renal_adequacy_status = float(creatinine) <= 1.5

    # last_treatment — latest therapy end date; falls back to latest start date
    _end_dates = [
        _parse_date_value(patient_info.first_line_end_date),
        _parse_date_value(patient_info.second_line_end_date),
        _parse_date_value(patient_info.later_end_date),
    ]
    _start_dates = [
        _parse_date_value(patient_info.first_line_start_date or patient_info.first_line_date),
        _parse_date_value(patient_info.second_line_start_date or patient_info.second_line_date),
        _parse_date_value(patient_info.later_start_date or patient_info.later_date),
    ]
    _valid_ends = [d for d in _end_dates if d]
    _valid_starts = [d for d in _start_dates if d]
    _lt_candidates = _valid_ends or _valid_starts
    if _lt_candidates:
        patient_info.last_treatment = max(_lt_candidates)


def _get_wearable_data(person: Person, snapshot: OmopSnapshot = None) -> dict:
    """Derive 30-day wearable summaries from OMOP Measurement/Observation rows."""
    snapshot = snapshot or _build_snapshot(person)
    data = {}

    # Prefer recent data (last 90 days) so stale/synthetic rows from years
    # ago don't anchor the 30-day window. Fall back to all data if nothing
    # exists within the recency window.
    recency_cutoff = (timezone.now() - timedelta(days=90)).date()
    _wearable_codes = set(WEARABLE_CONCEPT_CODE.values())

    def _wearable_measurements(date_filter=None):
        return [
            (getattr(m.measurement_concept, 'concept_code', None),
             m.measurement_source_value, m.measurement_date, m.value_as_number)
            for m in snapshot.measurements
            if m.value_as_number is not None
            and (getattr(m.measurement_concept, 'concept_code', None) in _wearable_codes
                 or m.measurement_source_value in _wearable_codes)
            and (date_filter is None or (m.measurement_date and m.measurement_date >= date_filter))
        ]

    def _wearable_observations(date_filter=None):
        return [
            (getattr(o.observation_concept, 'concept_code', None),
             o.observation_source_value, o.observation_date, o.value_as_number)
            for o in snapshot.observations
            if o.value_as_number is not None
            and (getattr(o.observation_concept, 'concept_code', None) in _wearable_codes
                 or o.observation_source_value in _wearable_codes)
            and (date_filter is None or (o.observation_date and o.observation_date >= date_filter))
        ]

    # Try recent data first; fall back to all data if nothing within 90 days
    measurement_rows = _wearable_measurements(recency_cutoff)
    observation_rows = _wearable_observations(recency_cutoff)
    if not measurement_rows and not observation_rows:
        measurement_rows = _wearable_measurements()
        observation_rows = _wearable_observations()

    if not measurement_rows and not observation_rows:
        return data

    # Key each row by the wearable LOINC it actually carries. A row may have an
    # unrelated concept_code (e.g. an unmapped source concept) yet a wearable
    # measurement_source_value — prefer whichever is a known wearable code so
    # the source_value fallback isn't shadowed by a present-but-unrelated concept.
    _wearable_codes = set(WEARABLE_CONCEPT_CODE.values())
    rows_by_code: dict[str, list[tuple[date, float]]] = {}
    # Measurement and Observation rows are merged into one index keyed by code,
    # so a metric is read the same way regardless of which table its concept's
    # domain routed it to.
    for concept_code, source_value, mdate, val in measurement_rows + observation_rows:
        code = concept_code if concept_code in _wearable_codes else source_value
        if code not in _wearable_codes:
            continue
        rows_by_code.setdefault(code, []).append((mdate, float(val)))

    def _metric_daily(metric_key):
        loinc_code = WEARABLE_CONCEPT_CODE[metric_key]
        lo, hi = WEARABLE_ARTIFACT_BOUNDS[metric_key]
        daily: dict[date, list[float]] = {}
        for mdate, val in rows_by_code.get(loinc_code, []):
            if lo <= val <= hi:
                daily.setdefault(mdate, []).append(val)
        return daily

    steps_daily = _metric_daily('steps')
    active_daily = _metric_daily('active_minutes')
    rhr_daily = _metric_daily('resting_hr')
    # SDNN and RMSSD are separate metrics with separate concepts — they must
    # never be merged into one summary. See #438.
    hrv_sdnn_daily = _metric_daily('hrv_sdnn')
    hrv_rmssd_daily = _metric_daily('hrv_rmssd')
    spo2_daily = _metric_daily('spo2')
    rr_daily = _metric_daily('respiratory_rate')
    vo2_daily = _metric_daily('vo2_max')
    distance_daily = _metric_daily('distance')
    walk_speed_daily = _metric_daily('walking_speed')
    walk_step_len_daily = _metric_daily('walking_step_length')
    walk_dbl_sup_daily = _metric_daily('walking_double_support_pct')
    walk_hr_daily = _metric_daily('walking_hr_avg')
    flights_daily = _metric_daily('flights_climbed')
    active_energy_daily = _metric_daily('active_energy')
    basal_energy_daily = _metric_daily('basal_energy')
    body_mass_daily = _metric_daily('body_mass')

    # Sleep needs no special-casing now that observation rows are merged above.
    sleep_daily = _metric_daily('sleep_duration')

    all_valid_days = sorted(
        steps_daily.keys() | active_daily.keys()
        | rhr_daily.keys() | hrv_sdnn_daily.keys() | hrv_rmssd_daily.keys()
        | spo2_daily.keys() | rr_daily.keys()
        | sleep_daily.keys() | vo2_daily.keys()
        | distance_daily.keys() | walk_speed_daily.keys()
        | walk_step_len_daily.keys() | walk_dbl_sup_daily.keys()
        | walk_hr_daily.keys() | flights_daily.keys()
        | active_energy_daily.keys() | basal_energy_daily.keys()
        | body_mass_daily.keys()
    )
    if not all_valid_days:
        return data

    def _window_valid_day_count(anchor_date):
        start = anchor_date - timedelta(days=29)
        return sum(start <= day <= anchor_date for day in all_valid_days)

    anchor_date = None
    for candidate in reversed(all_valid_days):
        if _window_valid_day_count(candidate) >= WEARABLE_MIN_VALID_DAYS:
            anchor_date = candidate
            break
    if anchor_date is None:
        anchor_date = all_valid_days[-1]

    window_start = anchor_date - timedelta(days=29)
    window_end = anchor_date
    data['wearable_last_sync_at'] = timezone.make_aware(
        timezone.datetime.combine(anchor_date, timezone.datetime.min.time())
    )

    def _within_window(daily_map):
        return {
            day: values
            for day, values in daily_map.items()
            if window_start <= day <= window_end
        }

    steps_daily = _within_window(steps_daily)
    active_daily = _within_window(active_daily)
    rhr_daily = _within_window(rhr_daily)
    hrv_sdnn_daily = _within_window(hrv_sdnn_daily)
    hrv_rmssd_daily = _within_window(hrv_rmssd_daily)
    spo2_daily = _within_window(spo2_daily)
    rr_daily = _within_window(rr_daily)
    sleep_daily = _within_window(sleep_daily)
    vo2_daily = _within_window(vo2_daily)
    distance_daily = _within_window(distance_daily)
    walk_speed_daily = _within_window(walk_speed_daily)
    walk_step_len_daily = _within_window(walk_step_len_daily)
    walk_dbl_sup_daily = _within_window(walk_dbl_sup_daily)
    walk_hr_daily = _within_window(walk_hr_daily)
    flights_daily = _within_window(flights_daily)
    active_energy_daily = _within_window(active_energy_daily)
    basal_energy_daily = _within_window(basal_energy_daily)
    body_mass_daily = _within_window(body_mass_daily)

    steps_totals = {d: sum(vs) for d, vs in steps_daily.items()}
    active_totals = {d: sum(vs) for d, vs in active_daily.items()}
    sleep_nightly = {d: sum(vs) for d, vs in sleep_daily.items()}

    # ---- Coverage ratio (union of all wearable metric days) ----------
    all_valid_days = (
        steps_totals.keys() | active_totals.keys()
        | rhr_daily.keys() | hrv_sdnn_daily.keys() | hrv_rmssd_daily.keys()
        | spo2_daily.keys() | rr_daily.keys()
        | sleep_nightly.keys() | vo2_daily.keys()
        | distance_daily.keys() | walk_speed_daily.keys()
        | walk_step_len_daily.keys() | walk_dbl_sup_daily.keys()
        | walk_hr_daily.keys() | flights_daily.keys()
        | active_energy_daily.keys() | basal_energy_daily.keys()
        | body_mass_daily.keys()
    )
    coverage = round(len(all_valid_days) / 30.0, 2)
    data['wearable_coverage_ratio_30d'] = coverage

    # ---- Median daily steps ------------------------------------------
    if steps_totals:
        data['median_daily_steps_30d'] = int(statistics.median(steps_totals.values()))

    # ---- Active minutes ----------------------------------------------
    if active_totals:
        data['active_minutes_per_day_30d'] = round(
            statistics.mean(active_totals.values()), 1
        )

    # ---- Activity trend (steps, first vs second half of window) ------
    # Default to insufficient_data; overwrite only when both halves have enough days.
    data['activity_trend_30d'] = 'insufficient_data'
    if steps_totals:
        mid = window_start + timedelta(days=15)
        first_half  = [v for d, v in steps_totals.items() if d < mid]
        second_half = [v for d, v in steps_totals.items() if d >= mid]
        if len(first_half) >= WEARABLE_MIN_VALID_DAYS and len(second_half) >= WEARABLE_MIN_VALID_DAYS:
            mean_first  = statistics.mean(first_half)
            mean_second = statistics.mean(second_half)
            if mean_first > 0:
                pct_change = (mean_second - mean_first) / mean_first * 100
            else:
                pct_change = 0.0
            if pct_change >= WEARABLE_TREND_IMPROVING_PCT:
                data['activity_trend_30d'] = 'improving'
            elif pct_change <= WEARABLE_TREND_DECLINING_PCT:
                data['activity_trend_30d'] = 'declining'
            else:
                data['activity_trend_30d'] = 'stable'

    # ---- Resting heart rate ------------------------------------------
    rhr_means = {d: statistics.mean(vs) for d, vs in rhr_daily.items()}
    if rhr_means:
        data['resting_heart_rate_avg_30d'] = int(round(statistics.mean(rhr_means.values())))

    # ---- HRV SDNN ----------------------------------------------------
    hrv_sdnn_means = {d: statistics.mean(vs) for d, vs in hrv_sdnn_daily.items()}
    if hrv_sdnn_means:
        data['hrv_sdnn_avg_30d'] = round(statistics.mean(hrv_sdnn_means.values()), 1)

    # ---- HRV RMSSD ---------------------------------------------------
    # Kept separate from SDNN deliberately: they are different statistics and
    # averaging them together would produce a number that is neither (#438).
    hrv_rmssd_means = {d: statistics.mean(vs) for d, vs in hrv_rmssd_daily.items()}
    if hrv_rmssd_means:
        data['hrv_rmssd_avg_30d'] = round(statistics.mean(hrv_rmssd_means.values()), 1)

    # ---- SpO2 minimum (single low reading is clinically significant) -
    all_spo2 = [v for vs in spo2_daily.values() for v in vs]
    if all_spo2:
        data['oxygen_saturation_min_30d'] = round(min(all_spo2), 2)

    # ---- SpO2 average (baseline oxygen saturation tracking) ----------
    spo2_means = {d: statistics.mean(vs) for d, vs in spo2_daily.items()}
    if spo2_means:
        data['oxygen_saturation_avg_30d'] = round(statistics.mean(spo2_means.values()), 2)

    # ---- Respiratory rate -------------------------------------------
    rr_means = {d: statistics.mean(vs) for d, vs in rr_daily.items()}
    if rr_means:
        data['respiratory_rate_avg_30d'] = round(statistics.mean(rr_means.values()), 1)

    # ---- Sleep duration (from Observation) ---------------------------
    if sleep_nightly:
        data['sleep_duration_hours_avg_30d'] = round(
            statistics.mean(sleep_nightly.values()), 1
        )

    # ---- VO2 Max -----------------------------------------------------
    vo2_means = {d: statistics.mean(vs) for d, vs in vo2_daily.items()}
    if vo2_means:
        data['vo2_max_avg_30d'] = round(statistics.mean(vo2_means.values()), 1)

    # ---- Distance (sum-per-day metrics) --------------------------------
    distance_totals = {d: sum(vs) for d, vs in distance_daily.items()}
    if distance_totals:
        data['distance_km_per_day_30d'] = round(statistics.mean(distance_totals.values()), 2)

    # ---- Walking speed --------------------------------------------------
    ws_means = {d: statistics.mean(vs) for d, vs in walk_speed_daily.items()}
    if ws_means:
        data['walking_speed_avg_30d'] = round(statistics.mean(ws_means.values()), 2)

    # ---- Walking step length --------------------------------------------
    wsl_means = {d: statistics.mean(vs) for d, vs in walk_step_len_daily.items()}
    if wsl_means:
        data['walking_step_length_avg_30d'] = round(statistics.mean(wsl_means.values()), 1)

    # ---- Walking double support % ---------------------------------------
    wds_means = {d: statistics.mean(vs) for d, vs in walk_dbl_sup_daily.items()}
    if wds_means:
        data['walking_double_support_pct_avg_30d'] = round(statistics.mean(wds_means.values()), 2)

    # ---- Walking heart rate ---------------------------------------------
    whr_means = {d: statistics.mean(vs) for d, vs in walk_hr_daily.items()}
    if whr_means:
        data['walking_hr_avg_30d'] = int(round(statistics.mean(whr_means.values())))

    # ---- Flights climbed (sum-per-day) ----------------------------------
    flights_totals = {d: sum(vs) for d, vs in flights_daily.items()}
    if flights_totals:
        data['flights_climbed_per_day_30d'] = round(statistics.mean(flights_totals.values()), 1)

    # ---- Active energy (sum-per-day) ------------------------------------
    ae_totals = {d: sum(vs) for d, vs in active_energy_daily.items()}
    if ae_totals:
        data['active_energy_per_day_30d'] = round(statistics.mean(ae_totals.values()), 1)

    # ---- Basal energy (sum-per-day) -------------------------------------
    be_totals = {d: sum(vs) for d, vs in basal_energy_daily.items()}
    if be_totals:
        data['basal_energy_per_day_30d'] = round(statistics.mean(be_totals.values()), 1)

    # ---- Body mass ------------------------------------------------------
    bm_means = {d: statistics.mean(vs) for d, vs in body_mass_daily.items()}
    if bm_means:
        data['body_mass_avg_30d'] = round(statistics.mean(bm_means.values()), 1)

    return data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_lymphocyte_doubling_time(alc_points):
    """Estimate lymphocyte doubling time (months) from serial ALC measurements."""
    if len(alc_points) < 2:
        return None
    first_date, first_alc = alc_points[0]
    last_date, last_alc = alc_points[-1]
    if float(last_alc) <= float(first_alc) or float(first_alc) <= 0:
        return None
    days = (last_date - first_date).days
    if days <= 0:
        return None
    months = days / 30.44
    ldt = months * math.log(2) / math.log(float(last_alc) / float(first_alc))
    return max(1, int(round(ldt)))
