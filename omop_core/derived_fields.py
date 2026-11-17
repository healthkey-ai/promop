"""The PatientRecord columns that derivation owns, as plain data.

Lives apart from ``patient_record_service`` because the provisional tables
(``patient_record_confidence``) are generated from this list when the models
load, and a model module cannot import a service that imports the models.
"""
from omop_core.services.genomics_catalog import patient_fields as _genomic_patient_fields

# Fields that are entirely derived from OMOP tables and must be reset before
# each refresh so deletions are reflected (not just additions).
_OMOP_DERIVED_FIELDS = [
    # Demographics. patient_age is a pure function of the birth date, so a value
    # left over from an earlier derivation is stale by construction and has to be
    # cleared. date_of_birth is deliberately not cleared: a manually supplied
    # DOB remains useful when Person has only the registration placeholder.
    'patient_age',
    'blood_type',
    # Person extension fields projected onto PatientRecord for compatibility.
    'email', 'phone_number', 'facility_name', 'validated', 'validated_by',
    'validation_date', 'suppress_demographics_for_others',
    # Disease / condition
    'disease', 'diagnosis_date', 'diagnosis_date_precision',
    'death_date', 'condition_clinical_status', 'disease_slug', 'disease_slugs',
    'active_infection_status', 'no_active_infection_status',
    'active_malignancies', 'no_other_active_malignancies',
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
    # Set only when the patient has confirmed the list is complete (#1924), so a
    # withdrawn confirmation must clear it on the next refresh.
    'therapy_history_complete',
    'concomitant_medications', 'medication_contexts',
    # Treatment assertions and summaries. These are set from dated OMOP rows
    # below; clearing them prevents legacy PatientRecord patches surviving a
    # source deletion.
    'first_line_outcome', 'first_line_intent', 'first_line_discontinuation_reason',
    'second_line_outcome', 'second_line_intent', 'second_line_discontinuation_reason',
    'later_outcome', 'later_intent', 'later_discontinuation_reason',
    'first_line_progression_date', 'second_line_progression_date', 'later_progression_date', 'pod24',
    'therapy_intent', 'reason_for_discontinuation', 'relapse_count',
    'treatment_refractory_status', 'washout_period_duration',
    'remission_duration_min', 'remission_duration',
    'line_of_therapy', 'prior_therapy',
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
    'prothrombin_time_uln', 'activated_partial_thromboplastin_time_uln',
    'able_to_swallow_tablets_capsules',
    'psa_ng_ml', 'phosphorus',
    # Remaining numeric Measurement projections (#504).
    'heartrate_variability', 'ejection_fraction', 'qtcf_value',
    'liver_enzyme_levels_ast', 'liver_enzyme_levels_alt',
    'liver_enzyme_levels_alp', 'liver_enzyme_levels',
    # Legacy aliases for deduplicated LOINC fields (issue #471).
    # These model columns still exist for backward compatibility; they are
    # populated from their canonical counterpart during derivation, converting
    # counts to the legacy cells/µL scale where needed. See below.
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
    'kappa_flc', 'lambda_flc', 'clonal_plasma_cells', 'kappa_lambda_ratio',
    'involved_uninvolved_ratio',
    # MM boolean/coded fields (derived by _get_mm_specific_data)
    'plasma_cell_leukemia', 'bone_lesions', 'meets_crab', 'meets_slim',
    # MM cytogenetics + SCT (derived by _get_sct_cytogenetic_data)
    'cytogenetic_markers', 'sct_date', 'sct_date_precision',
    'stem_cell_transplant_history', 'sct_eligibility',
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
    'flipi_risk_category', 'number_of_nodal_sites', 'bulky_disease', 'b_symptoms',
    'b_symptom_fever', 'b_symptom_night_sweats', 'b_symptom_weight_loss',
    'gelf_criteria_options', 'ldh_upper_limit_normal',
    'transformed_to_dlbcl', 'dlbcl_transformation_date', 'dlbcl_transformation_date_precision',
    'post_transformation_outcome',
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

# Genomic markers are catalogue data; they are derived the same way.
_OMOP_DERIVED_FIELDS.extend(_genomic_patient_fields())
