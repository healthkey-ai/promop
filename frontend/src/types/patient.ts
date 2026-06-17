export interface User {
  id: number;
  username: string;
  email: string;
  first_name: string;
  last_name: string;
}

export interface PatientInfo {
  id?: number;
  person?: number;
  email?: string;
  
  // General
  patient_age?: number;
  gender?: string;
  weight_kg?: number;
  height_cm?: number;
  bmi?: number;
  race?: string | null;
  ethnicity?: string | null;
  systolic_bp?: number;
  diastolic_bp?: number;
  location?: string;
  postal_code?: string;
  disease?: string;
  stage?: string;
  karnofsky_performance_status?: number;
  ecog_performance_status?: number;
  no_other_active_malignancies?: boolean;
  no_active_infection_status?: boolean;
  preexisting_conditions?: string[];
  peripheral_neuropathy_grade?: number;
  
  // Follicular Lymphoma specific
  gelf_criteria?: string;
  flipi_score?: number;
  tumor_grade?: string;
  
  // Multiple Myeloma specific
  cytogenic_markers?: string;
  molecular_markers?: string;
  plasma_cell_leukemia?: boolean;
  progression?: string;
  measurable_disease_imwg?: boolean;
  mrd_status?: string | null;
  stem_cell_transplant_history?: string[] | null;
  sct_date?: string | null;
  sct_eligibility?: string[] | null;
  // Treatment
  prior_therapy?: string;
  prior_lines_of_therapy?: number;
  therapy_lines_count?: number;
  relapse_count?: number;
  refractory_status?: string;
  treatment_refractory_status?: string;
  prior_treatments?: string;
  prior_chemotherapy?: boolean;
  prior_radiation?: boolean;
  prior_surgery?: boolean;
  prior_immunotherapy?: boolean;
  prior_targeted_therapy?: boolean;
  current_medications?: string;
  allergies?: string;
  
  // Therapy Lines
  first_line_therapy?: string;
  first_line_date?: string;
  first_line_start_date?: string;
  first_line_end_date?: string;
  first_line_intent?: string;
  first_line_discontinuation_reason?: string;
  first_line_outcome?: string;
  second_line_therapy?: string;
  second_line_date?: string;
  second_line_start_date?: string;
  second_line_end_date?: string;
  second_line_intent?: string;
  second_line_discontinuation_reason?: string;
  second_line_outcome?: string;
  later_therapy?: string;
  later_line_therapy?: string;  // UI uses this name
  // HemOnc concept_id fields
  first_line_therapy_id?: number | null;
  second_line_therapy_id?: number | null;
  later_therapy_ids?: number[] | null;
  first_line_therapy_display?: string | null;
  second_line_therapy_display?: string | null;
  later_therapy_display?: string[] | null;
  later_date?: string;
  later_line_date?: string;  // UI uses this name
  later_start_date?: string;
  later_end_date?: string;
  later_intent?: string;
  later_discontinuation_reason?: string;
  later_outcome?: string;
  later_line_outcome?: string;  // UI uses this name
  
  // Supportive Therapy
  supportive_therapies?: string;
  supportive_therapy_start_date?: string;
  supportive_therapy_end_date?: string;
  supportive_therapy_intent?: string;
  
  // Planned Therapies
  planned_therapies?: string;
  
  // Blood Markers
  hemoglobin_g_dl?: number;
  hematocrit_percent?: number;
  wbc_count_thousand_per_ul?: number;
  rbc_million_per_ul?: number;
  platelet_count_thousand_per_ul?: number;
  anc_thousand_per_ul?: number;
  alc_thousand_per_ul?: number;
  amc_thousand_per_ul?: number;
  serum_creatinine_mg_dl?: number;
  creatinine_clearance_ml_min?: number;
  creatinine_mg_dl?: number;
  egfr_ml_min_173m2?: number;
  bun_mg_dl?: number;
  sodium_meq_l?: number;
  potassium_meq_l?: number;
  serum_calcium_mg_dl?: number;
  calcium_mg_dl?: number;
  magnesium_mg_dl?: number;
  
  // Labs
  ldh_u_l?: number;
  pulmonary_function_test_result?: boolean;
  bone_imaging_result?: boolean;
  hiv_status?: boolean;
  hepatitis_b_status?: boolean;
  hepatitis_c_status?: boolean;
  no_hiv_status?: boolean;
  no_hepatitis_b_status?: boolean;
  no_hepatitis_c_status?: boolean;
  
  bilirubin_total_mg_dl?: number;
  alt_u_l?: number;
  ast_u_l?: number;
  alkaline_phosphatase_u_l?: number;
  albumin_g_dl?: number;
  troponin_ng_ml?: number;
  bnp_pg_ml?: number;
  glucose_mg_dl?: number;
  hba1c_percent?: number;
  inr?: number;
  pt_seconds?: number;
  ptt_seconds?: number;
  cea_ng_ml?: number;
  ca19_9_u_ml?: number;
  psa_ng_ml?: number;
  
  // Behavior
  able_to_consent?: boolean;
  has_caregiver?: boolean;
  using_contraceptive?: boolean;
  is_pregnant?: boolean;
  pregnancy_test_result?: string;
  is_lactating?: boolean;
  tobacco_use_status?: string;
  recreational_drug_use?: string;
  mental_health_disorders?: string;
  geographic_risk_factors?: string;
  occupational_risk_factors?: string;
  english_speak_understand?: boolean;
  english_read_write?: boolean;
  spanish_speak_understand?: boolean;
  spanish_read_write?: boolean;
  
  // Behavior tab - Lifestyle
  smoking_status?: string;
  pack_years?: number;
  alcohol_use?: string;
  drinks_per_week?: number;
  exercise_frequency?: string;
  exercise_minutes_per_week?: number;
  diet_type?: string;
  
  // Behavior tab - Sleep & Wellbeing
  sleep_hours_per_night?: number;
  sleep_quality?: string;
  stress_level?: string;
  social_support?: string;
  
  // Behavior tab - Socioeconomic
  employment_status?: string;
  education_level?: string;
  marital_status?: string;
  insurance_type?: string;
  number_of_dependents?: number;
  annual_household_income?: number;
  
  // Cancer Assessment Fields
  ecog_assessment_date?: string;
  test_methodology?: string;
  test_date?: string;
  test_specimen_type?: string;
  report_interpretation?: string;
  oncotype_dx_score?: number;
  ki67_percentage?: number;
  androgen_receptor_status?: string;
  
  // Additional Lab Values
  ldh?: number;
  alkaline_phosphatase?: number;
  magnesium?: number;
  phosphorus?: number;
  
  // Reproductive Health
  pregnancy_test_date?: string;
  pregnancy_test_result_value?: string;
  contraceptive_use?: boolean;
  
  // Consent and Support
  consent_capability?: boolean;
  caregiver_availability_status?: boolean;
  
  // Mental Health and Substance Use
  no_mental_health_disorder_status?: boolean;
  no_substance_use_status?: boolean;
  substance_use_details?: string;
  
  // Geographic Exposure
  no_geographic_exposure_risk?: boolean;
  geographic_exposure_risk_details?: string;

  // Breast cancer staging
  measurable_disease_by_recist_status?: boolean;
  bone_only_metastasis_status?: boolean;
  tumor_size?: number;
  tumor_stage?: string;
  nodes_stage?: string;
  staging_modalities?: string;
  distant_metastasis_stage?: string;

  // CLL fields
  clonal_bone_marrow_b_lymphocytes?: number;

  // HealthTree parity fields
  diagnosis_date?: string | null;
  condition_clinical_status?: string | null;
  disease_slug?: string | null;
  validated?: boolean | null;
  validated_by?: string | null;
  validation_date?: string | null;
  phone_number?: string | null;
  facility_name?: string | null;
  prior_procedures?: Array<{procedure: string; date?: string | null; concept_id?: number}> | null;
}

export interface PatientDocument {
  id?: number;
  person: number;
  doc_type: 'FISH' | 'GEP' | 'NGS' | 'CYTOMETRY' | 'CYTOGENETICS' | 'LAB_RESULTS' |
            'FULL_MEDICAL_RECORDS' | 'MRD' | 'BONE_MARROW' | 'CONSENT' | 'IMAGING' | 'OTHER';
  title?: string | null;
  file_url?: string | null;
  file_name?: string | null;
  verified?: boolean;
  uploaded_at?: string;
}

export interface PatientMessage {
  id: number;
  patient_user: number;
  subject: string;
  message: string;
  sender_is_patient: boolean;
  created_at: string;
  read_at?: string;
}

export interface PatientConsent {
  id: number;
  patient_user: number;
  consent_type: 'data_sharing' | 'clinical_trial' | 'research';
  consent_granted: boolean;
  consent_date?: string;
}