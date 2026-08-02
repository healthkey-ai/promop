"""
patient_record_service.py — Reusable service for deriving PatientRecord from OMOP tables.

Usage:
    from omop_core.services.patient_record_service import refresh_patient_record
    patient_info = refresh_patient_record(person)
"""

import math
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from django.db import models
from django.db.models import DateTimeField
from django.db.models.functions import Cast, Coalesce
from django.db import transaction
from django.utils import timezone
from omop_core.models import (
    Person, PatientRecord, ConditionOccurrence, Concept,
    Measurement, Observation, DrugExposure, Location, ProcedureOccurrence,
    Death, ConceptRelationship,
)
from omop_core.services.concept_cache import concept_by_id as _cc_by_id, concept_by_loinc as _cc_by_loinc
from omop_core.services.mappings import (
    WEARABLE_LOINC, WEARABLE_ARTIFACT_BOUNDS, WEARABLE_MIN_VALID_DAYS,
    WEARABLE_TREND_IMPROVING_PCT, WEARABLE_TREND_DECLINING_PCT,
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

# Bump this whenever aggregation or computation logic changes in any section
# extractor or in _compute_derived_fields.  See DERIVATION_CHANGELOG.md.
DERIVATION_VERSION = 1

# Fields that are entirely derived from OMOP tables and must be reset before
# each refresh so deletions are reflected (not just additions).
_OMOP_DERIVED_FIELDS = [
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
    'first_line_component_class_ids', 'second_line_component_class_ids',
    'later_component_class_ids', 'therapy_component_class_ids',
    'therapy_ids_provenance',
    'therapy_lines_count', 'last_treatment',
    'concomitant_medications',
    # Legacy labs (derived via name-based Measurement lookup)
    'hemoglobin_level', 'hemoglobin_level_units',
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
    'bilirubin_total_mg_dl', 'alt_u_l', 'ast_u_l', 'alkaline_phosphatase_u_l',
    'albumin_g_dl', 'total_protein', 'troponin_ng_ml', 'bnp_pg_ml',
    'glucose_mg_dl', 'hba1c_percent', 'ldh_u_l',
    # Other markers (LOINC-derived)
    'beta2_microglobulin', 'c_reactive_protein', 'esr',
    # MM disease burden (LOINC-derived)
    'monoclonal_protein_serum', 'monoclonal_protein_urine',
    'kappa_flc', 'lambda_flc', 'clonal_plasma_cells',
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
    # Behavior / lifestyle
    'smoking_status', 'pack_years', 'alcohol_use', 'drinks_per_week',
    'exercise_frequency', 'exercise_minutes_per_week', 'diet_type',
    'sleep_hours_per_night', 'sleep_quality', 'stress_level', 'social_support',
    'employment_status', 'education_level', 'marital_status', 'insurance_type',
    'number_of_dependents', 'annual_household_income',
    # Staging
    'stage', 'tumor_stage', 'nodes_stage', 'distant_metastasis_stage', 'staging_modalities',
    'metastatic_status', 'bone_only_metastasis_status',
    # Biopsy
    'histologic_type', 'biopsy_grade',
    # Genomics
    'genetic_mutations',
    # CLL
    'absolute_lymphocyte_count', 'serum_beta2_microglobulin_level',
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
    'resting_heart_rate_avg_30d', 'hrv_sdnn_avg_30d',
    'oxygen_saturation_min_30d', 'respiratory_rate_avg_30d',
    'sleep_duration_hours_avg_30d',
]


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
    '1742-6':  ('alt_u_l',                        int),
    '1920-8':  ('ast_u_l',                        int),
    '6768-6':  ('alkaline_phosphatase_u_l',       int),
    '1751-7':  ('albumin_g_dl',                   float),
    '2885-2':  ('total_protein',                  float),
    '10839-9': ('troponin_ng_ml',                 float),
    '42637-9': ('bnp_pg_ml',                      int),
    '4548-4':  ('hba1c_percent',                  float),
    '2532-0':  ('ldh_u_l',                        int),
    # Other markers
    '1952-1':  ('beta2_microglobulin',            float),
    '1988-5':  ('c_reactive_protein',             float),
    '30341-2': ('esr',                            int),
    # MM-specific disease burden labs
    '51435-6': ('monoclonal_protein_serum',       float),  # Serum M-spike (M-protein)
    '32730-5': ('monoclonal_protein_urine',       float),  # Urine M-spike 24h
    '33944-8': ('kappa_flc',                      float),  # Kappa free light chains
    '33945-5': ('lambda_flc',                     float),  # Lambda free light chains
    '26098-4': ('clonal_plasma_cells',            float),  # Plasma cells % in bone marrow
}

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


def _clear_derived_fields(patient_info: PatientRecord) -> None:
    """Reset all OMOP-derived fields to None so deletions are reflected."""
    for field in _OMOP_DERIVED_FIELDS:
        if hasattr(patient_info, field):
            default = [] if field in (
                'prior_procedures', 'later_therapies', 'genetic_mutations', 'later_therapy_ids',
                'first_line_component_ids', 'second_line_component_ids',
                'later_component_ids', 'therapy_component_ids',
                'first_line_component_class_ids', 'second_line_component_class_ids',
                'later_component_class_ids', 'therapy_component_class_ids',
                'stem_cell_transplant_history', 'sct_eligibility',
            ) else None
            setattr(patient_info, field, default)


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

        # Populate all sections
        for section_fn in [
            _get_demographics,
            _get_location_data,
            _get_disease_data,
            _get_treatment_data,
            _get_vitals_data,
            _get_biomarker_data,
            _get_staging_data,
            _get_social_data,
            _get_behavior_data,
            _get_infection_data,
            _get_assessment_data,
            _get_laboratory_data,
            _get_performance_data,
            _get_genetic_mutations,
            _get_cll_data,
            _get_lymphoma_data,
            _get_bc_clinical_data,
            _get_prior_procedures,
            _get_wearable_data,
            _get_mm_specific_data,
            _get_sct_cytogenetic_data,
        ]:
            for field, value in section_fn(person).items():
                setattr(patient_info, field, value)

        _compute_derived_fields(patient_info)

        patient_info.derivation_version = DERIVATION_VERSION
        patient_info.derived_at = timezone.now()

        patient_info.save()
        return patient_info


# ---------------------------------------------------------------------------
# Section extractors — each returns a dict of {field_name: value}
# ---------------------------------------------------------------------------

def _get_demographics(person: Person) -> dict:
    data = {}

    death = Death.objects.filter(person=person).only('death_date').first()
    if death:
        data['death_date'] = death.death_date

    if person.year_of_birth:
        today = date.today()
        data['patient_age'] = today.year - person.year_of_birth

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

    return data


def _get_location_data(person: Person) -> dict:
    data = {}

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


def _get_disease_data(person: Person) -> dict:
    data = {}

    # Most-recent oncologic condition — match common OMOP oncology terms
    from django.db.models import Q
    cancer_condition = ConditionOccurrence.objects.filter(
        person=person,
    ).filter(
        Q(condition_concept__concept_name__icontains='cancer')
        | Q(condition_concept__concept_name__icontains='neoplasm')
        | Q(condition_concept__concept_name__icontains='malignant')
        | Q(condition_concept__concept_name__icontains='lymphoma')
        | Q(condition_concept__concept_name__icontains='leukemia')
        | Q(condition_concept__concept_name__icontains='myeloma')
        | Q(condition_concept__concept_name__icontains='carcinoma')
        | Q(condition_concept__concept_name__icontains='sarcoma')
        | Q(condition_concept__concept_name__icontains='tumor')
    ).order_by('-condition_start_date').first()

    if cancer_condition:
        data['disease'] = _canonicalize_disease(cancer_condition.condition_concept.concept_name)
        if cancer_condition.condition_start_date:
            data['diagnosis_date'] = cancer_condition.condition_start_date

    # Any condition for diagnosis_date fallback (most-recent)
    if 'diagnosis_date' not in data:
        earliest = ConditionOccurrence.objects.filter(
            person=person,
            condition_start_date__isnull=False,
        ).order_by('condition_start_date').first()
        if earliest:
            data['diagnosis_date'] = earliest.condition_start_date

    # condition_clinical_status — from condition_status_concept
    most_recent_condition = ConditionOccurrence.objects.filter(
        person=person,
    ).order_by('-condition_start_date').first()

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


def _get_treatment_data(person: Person) -> dict:
    data = {}

    drug_exposures = list(
        DrugExposure.objects
        .filter(person=person)
        .select_related('drug_concept')
        .order_by('drug_exposure_start_date', 'drug_exposure_id')
    )

    if not drug_exposures:
        return data

    recent_drugs = list(reversed(drug_exposures[-10:]))

    current_meds = []
    for drug in recent_drugs[:5]:
        if drug.drug_concept:
            current_meds.append(drug.drug_concept.concept_name)
    if current_meds:
        data['concomitant_medications'] = ', '.join(current_meds)

    # Try Episode-based therapy line grouping first
    try:
        from omop_oncology.models import Episode
        episodes = Episode.objects.filter(person=person).select_related('episode_source_concept').order_by('episode_number')
        if episodes.exists():
            return _get_treatment_data_from_episodes(person, data, episodes, drug_exposures)
    except Exception:
        pass

    # No Episodes persisted yet — derive from the single LOT inference engine in
    # read-only (dry_run) mode, so the same grouping algorithm the enrich/import
    # steps use to *persist* Episodes also drives derivation. No OMOP rows are
    # written here; refresh_patient_record stays read-only.
    from omop_core.services.lot_inference_service import infer_lot_for_person
    lots = infer_lot_for_person(person, dry_run=True)
    _apply_inferred_lots(data, lots)
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
    _expand_component_ids), follow HemOnc
    'Component --[Is a]--> Component Class' edges transitively, keeping only
    targets whose concept_class_id is 'Component Class' (the drug-class
    concepts). Sub-class chains (narrow class --Is a--> broader class) are
    followed so a patient carries *every* applicable class, not just the
    narrowest — matching how trial type criteria are authored.

    Bounded BFS: one query per hop, a visited set prevents cycles, and
    _CLASS_MAX_HOPS backstops any pathological graph. Returns a set of
    class concept_ids (never includes the seed component ids themselves).
    """
    seen = {int(c) for c in (component_ids or ()) if c}
    if not seen:
        return set()
    classes = set()
    frontier = seen
    for _ in range(_CLASS_MAX_HOPS):
        hits = set(
            ConceptRelationship.objects.filter(
                concept_1_id__in=frontier,
                relationship_id=_TYPE_RELATIONSHIP_ID,
                concept_2__concept_class_id=_TYPE_CONCEPT_CLASS_ID,
            ).values_list('concept_2_id', flat=True)
        )
        new = hits - seen
        if not new:
            break
        classes |= new
        seen |= new
        frontier = new
    return classes


def _regimen_from_exposures(exposure_ids, de_info_by_id):
    """Resolve (display_name, concept_id) from a LOT's backing DrugExposures.

    de_info_by_id maps drug_exposure_id -> (concept_id, vocabulary_id, name).

    If an exposure's drug_concept is itself a HemOnc regimen concept (e.g. a
    regimen-level row from BC therapy backfill), use it directly. Otherwise
    mirror the Episode-derivation naming: a HemOnc regimen concept resolved from
    the drug-name set, else the canonical regimen name, else the joined original
    concept names (preserving casing).
    """
    infos = [de_info_by_id[e] for e in exposure_ids if e in de_info_by_id]
    if not infos:
        return 'Unknown', None
    for concept_id, vocab_id, name in infos:
        if concept_id and vocab_id == 'HemOnc':
            return name, concept_id
    display_names = [name for _, _, name in infos]
    norm_key = {normalize_drug_name(n).lower().strip() for n in display_names}
    concept_id = get_regimen_concept_id(norm_key)
    if concept_id:
        concept = _cc_by_id(concept_id)
        if concept:
            return concept.concept_name, concept_id
    regimen_name = get_regimen_name(norm_key)
    if regimen_name:
        return regimen_name, concept_id
    return ' + '.join(display_names), concept_id


def _apply_inferred_lots(data: dict, lots) -> None:
    """Map in-memory inferred LOTs onto first/second/later therapy fields."""
    data['therapy_lines_count'] = len(lots)

    # Bulk-resolve (concept_id, vocabulary_id, name) for every exposure across
    # all lots. concept.vocabulary_id is the FK's string PK — no extra join.
    exp_ids = [eid for lot in lots for eid in lot.exposure_ids]
    de_info_by_id = {}
    if exp_ids:
        for de in (DrugExposure.objects
                   .filter(drug_exposure_id__in=exp_ids)
                   .select_related('drug_concept')):
            if de.drug_concept:
                de_info_by_id[de.drug_exposure_id] = (
                    de.drug_concept.concept_id,
                    de.drug_concept.vocabulary_id,
                    de.drug_concept.concept_name,
                )
            else:
                de_info_by_id[de.drug_exposure_id] = (None, None, de.drug_source_value or 'Unknown')

    later = []
    later_ids = []
    later_components = set()
    all_components = set()
    later_classes = set()
    all_classes = set()
    for lot in lots:
        name, concept_id = _regimen_from_exposures(lot.exposure_ids, de_info_by_id)
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
            data['first_line_component_class_ids'] = sorted(classes)
            data['first_line_date'] = start
            data['first_line_start_date'] = start
            data['first_line_end_date'] = end
        elif lot.lot_number == 2:
            data['second_line_therapy'] = name
            data['second_line_therapy_id'] = concept_id
            data['second_line_component_ids'] = sorted(components)
            data['second_line_component_class_ids'] = sorted(classes)
            data['second_line_date'] = start
            data['second_line_start_date'] = start
            data['second_line_end_date'] = end
        else:  # lot_number >= 3
            later_components |= components
            later_classes |= classes
            if not data.get('later_therapy'):
                data['later_therapy'] = name
                data['later_date'] = start
                data['later_start_date'] = start
                data['later_end_date'] = end
            # Carry the actual line number and per-line concept_id (may be None
            # when the regimen did not resolve) so the structured
            # lines_of_therapy payload keeps the true line number, pairs each
            # later line with its own id, and never drops an unresolved line.
            later.append({'lineNumber': lot.lot_number, 'therapy': name,
                          'startDate': start, 'endDate': end,
                          'concept_id': concept_id})
            if concept_id:
                later_ids.append(concept_id)
    if later:
        data['later_therapies'] = later
    if later_ids:
        data['later_therapy_ids'] = later_ids
    if later_components:
        data['later_component_ids'] = sorted(later_components)
    if all_components:
        data['therapy_component_ids'] = sorted(all_components)
    if later_classes:
        data['later_component_class_ids'] = sorted(later_classes)
    if all_classes:
        data['therapy_component_class_ids'] = sorted(all_classes)


def _get_treatment_data_from_episodes(person, data, episodes, drug_exposures):
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
            normalize_drug_name(de.drug_concept.concept_name)
            for de in drugs_in_episode
            if de.drug_concept
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
        src_concept = episode.episode_source_concept
        if (episode.episode_source_concept_id and src_concept and
                getattr(src_concept, 'vocabulary_id', None) == 'HemOnc'):
            concept_id = episode.episode_source_concept_id
        elif drug_name_set:
            concept_id = get_regimen_concept_id(drug_name_set)
        # Final fallback: treat each drug_source_value as an abbreviated regimen name
        if not concept_id and source_value_set:
            for sv in source_value_set:
                cid = get_regimen_concept_id_by_name(sv)
                if cid:
                    concept_id = cid
                    break

        if concept_id:
            needed_concept_ids.add(concept_id)
        episode_rows.append((episode, drugs_in_episode, concept_id, source_value_set))

    # ── Bulk-fetch Concept names in one query ──────────────────────────────
    # Concept rows already loaded via select_related are re-used directly.
    concept_name_map: dict = {
        ep.episode_source_concept_id: ep.episode_source_concept.concept_name
        for ep in episodes
        if ep.episode_source_concept_id and ep.episode_source_concept
    }
    to_fetch = needed_concept_ids - set(concept_name_map)
    if to_fetch:
        concept_name_map.update(
            (c.concept_id, c.concept_name)
            for c in Concept.objects.filter(concept_id__in=to_fetch).only('concept_id', 'concept_name')
        )

    # ── Second pass: populate data dict ───────────────────────────────────
    therapy_line_numbers = set()
    later_components = set()
    all_components = set()
    later_classes = set()
    all_classes = set()

    for episode, drugs_in_episode, concept_id, source_value_set in episode_rows:
        lot = episode.episode_number
        if lot is None:
            continue
        therapy_line_numbers.add(lot)

        # Nullify dangling FK concept_ids (not in Concept table)
        if concept_id and concept_id not in concept_name_map:
            concept_id = None

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
            _drug_cnames = [normalize_drug_name(de.drug_concept.concept_name) for de in drugs_in_episode if de.drug_concept]
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
            data['first_line_component_class_ids'] = sorted(classes)
            data['first_line_date'] = start_date
            data['first_line_start_date'] = start_date
            if end_date:
                data['first_line_end_date'] = end_date
        elif lot == 2:
            data['second_line_therapy'] = drug_names
            data['second_line_therapy_id'] = concept_id
            data['second_line_component_ids'] = sorted(components)
            data['second_line_component_class_ids'] = sorted(classes)
            data['second_line_date'] = start_date
            data['second_line_start_date'] = start_date
            if end_date:
                data['second_line_end_date'] = end_date
        elif lot >= 3:
            later_components |= components
            later_classes |= classes
            later = data.get('later_therapies', [])
            # Keep the true episode line number and per-line concept_id (may be
            # None for an unresolved regimen) so lines_of_therapy renders the
            # correct line number and pairs each later line with its own id.
            later.append({'lineNumber': lot, 'therapy': drug_names,
                          'startDate': start_date, 'endDate': end_date,
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
    if later_classes:
        data['later_component_class_ids'] = sorted(later_classes)
    if all_classes:
        data['therapy_component_class_ids'] = sorted(all_classes)

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
    outcome_obs = (
        Observation.objects
        .filter(
            person=person,
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

    return data


def _get_vitals_data(person: Person) -> dict:
    data = {}

    vital_sign_concepts = {
        'systolic_bp': '8480-6',
        'diastolic_bp': '8462-4',
        'heart_rate': '8867-4',
        'weight': '29463-7',
        'height': '8302-2',
        'temperature': '8310-5',
    }

    measurements = (
        Measurement.objects
        .filter(
            person=person,
            measurement_concept__concept_code__in=vital_sign_concepts.values(),
            value_as_number__isnull=False,
        )
        .select_related('measurement_concept')
        .order_by('-measurement_date')
    )
    first_by_code = {}
    for measurement in measurements:
        first_by_code.setdefault(measurement.measurement_concept.concept_code, measurement)

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
    '85337-4',  # PD-L1 tumor cells
    '16112-5',  # Estrogen receptor
    '16113-3',  # Progesterone receptor
    '48676-1',  # HER2
    '85319-2',  # Ki-67
    '85336-6',  # PD-L1 immune cells
    '96893-3',  # PD-L1 combined positive score
    '44648-4',  # Biopsy/Nottingham grade
    '76690-7',  # Menopausal status
})
_BIOMARKER_OBS_LOINCS = frozenset({'76690-7', '44667-4'})
_HISTOLOGIC_TYPE_LOINCS = frozenset({'59847-4'})
_GENETIC_MUTATION_LOINCS = {
    '21636-6': 'BRCA1',
    '21637-4': 'BRCA2',
    '21667-1': 'TP53',
    '48013-7': 'KRAS',
    '62862-8': 'EGFR',
    '62318-1': 'PIK3CA',
}


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
    if observation.observation_concept and observation.observation_concept.concept_code:
        return observation.observation_concept.concept_code
    return observation.observation_source_value


def _get_biomarker_data(person: Person) -> dict:
    from django.db.models import Q
    data = {}

    measurements = list(
        Measurement.objects
        .filter(person=person)
        .filter(
            Q(measurement_concept__concept_code__in=_BIOMARKER_MEASUREMENT_LOINCS)
            | Q(measurement_concept__concept_code__in=_HISTOLOGIC_TYPE_LOINCS)
            | Q(measurement_source_value__in=_BIOMARKER_MEASUREMENT_LOINCS)
            | Q(measurement_source_value__in=_HISTOLOGIC_TYPE_LOINCS)
        )
        .select_related('measurement_concept', 'value_as_concept')
        .order_by('-measurement_date')
    )
    observations = list(
        Observation.objects
        .filter(person=person)
        .filter(
            Q(observation_concept__concept_code__in=_BIOMARKER_OBS_LOINCS)
            | Q(observation_concept__concept_code__in=_HISTOLOGIC_TYPE_LOINCS)
            | Q(observation_source_value__in=_BIOMARKER_OBS_LOINCS)
            | Q(observation_source_value__in=_HISTOLOGIC_TYPE_LOINCS)
            | Q(observation_concept__concept_name__icontains='homologous recombination')
            | Q(observation_concept__concept_name__icontains='bone only metastas')
            | Q(observation_concept__concept_name__icontains='histologic')
        )
        .select_related('observation_concept', 'value_as_concept')
        .order_by('-observation_date')
    )

    pdl1_test = next((m for m in measurements if _measurement_code(m) == '85337-4'), None)
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

    # PD-L1 immune cell percentage — LOINC 85336-6
    pdl1_ic_m = _first_m('85336-6')
    if pdl1_ic_m and pdl1_ic_m.value_as_number is not None:
        data['pd_l1_ic_percentage'] = int(pdl1_ic_m.value_as_number)

    # PD-L1 combined positive score — LOINC 96893-3
    pdl1_cps_m = _first_m('96893-3')
    if pdl1_cps_m and pdl1_cps_m.value_as_number is not None:
        data['pd_l1_combined_positive_score'] = int(pdl1_cps_m.value_as_number)

    # Biopsy (Nottingham) grade — LOINC 44648-4
    biopsy_m = _first_m('44648-4')
    if biopsy_m and biopsy_m.value_as_number is not None:
        data['biopsy_grade'] = int(biopsy_m.value_as_number)

    # Menopausal status — LOINC 76690-7 (Measurement first, then Observation)
    menopause_m = _first_m('76690-7')
    if menopause_m:
        val = menopause_m.value_as_string
        if not val and menopause_m.value_as_concept:
            val = menopause_m.value_as_concept.concept_name
        if val:
            data['menopausal_status'] = val
    else:
        menopause_obs = next((obs for obs in observations if _observation_code(obs) == '76690-7'), None)
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


# 21908-9-riss is a non-standard code the MM generator uses for R-ISS stage
# (it mis-resolves to an unrelated concept on import, so it is matched by
# source_value only).
_STAGING_LOINCS = frozenset({'21908-9', '21908-9-riss', '21905-5', '21906-3', '21901-4'})


def _get_staging_data(person: Person) -> dict:
    """Derive TNM staging and overall stage group from OMOP Measurement/Observation rows.

    Reads staging data by LOINC code matched on either the concept code
    (OMOP-native path) or the source_value (FHIR upload path), across both
    Measurement and Observation rows.
    """
    from django.db.models import Q
    data = {}

    measurements = list(
        Measurement.objects
        .filter(person=person)
        .filter(
            Q(measurement_concept__concept_code__in=_STAGING_LOINCS)
            | Q(measurement_source_value__in=_STAGING_LOINCS)
        )
        .select_related('measurement_concept')
        .order_by('-measurement_date')
    )
    observations = list(
        Observation.objects
        .filter(person=person)
        .filter(
            Q(observation_concept__concept_code__in=_STAGING_LOINCS)
            | Q(observation_source_value__in=_STAGING_LOINCS)
        )
        .select_related('observation_concept')
        .order_by('-observation_date')
    )

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


def _get_bc_clinical_data(person: Person) -> dict:
    """Derive breast-cancer clinical assessment fields from OMOP Observation rows.

    Covers CTCAE toxicity/neuropathy grades stored as clinical assessments.
    """
    data = {}

    observations = (
        Observation.objects.filter(person=person)
        .select_related('observation_concept')
        .order_by('-observation_date')
    )

    # Peripheral neuropathy CTCAE grade — concept name match
    pn_obs = observations.filter(
        observation_concept__concept_name__icontains='peripheral neuropathy',
    ).first()
    if pn_obs and pn_obs.value_as_number is not None:
        data['peripheral_neuropathy_grade'] = int(pn_obs.value_as_number)

    # Toxicity grade — CTCAE adverse event grade
    tox_obs = observations.filter(
        observation_concept__concept_name__icontains='toxicity grade',
    ).first()
    if not tox_obs:
        tox_obs = observations.filter(
            observation_concept__concept_name__icontains='adverse event',
        ).first()
    if tox_obs and tox_obs.value_as_number is not None:
        data['toxicity_grade'] = int(tox_obs.value_as_number)

    return data


def _get_social_data(person: Person) -> dict:
    data = {}

    observations = Observation.objects.filter(person=person)

    employment_obs = observations.filter(
        observation_concept__concept_code__in=['224362002', '160903007']
    )
    if employment_obs.exists():
        data['no_pre_existing_conditions'] = employment_obs.first().value_as_string

    insurance_obs = observations.filter(
        observation_concept__concept_code__in=['408729009']
    )
    if insurance_obs.exists():
        data['concomitant_medication_details'] = insurance_obs.first().value_as_string

    return data


def _get_behavior_data(person: Person) -> dict:
    data = {}

    observations = Observation.objects.filter(person=person)
    measurements = (
        Measurement.objects.filter(person=person)
        .select_related('measurement_concept')
        .order_by('-measurement_date')
    )

    tobacco_obs = observations.filter(
        observation_concept__concept_code__in=['266919005', '8517006', '77176002']
    )

    for obs in tobacco_obs:
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

    for measurement in measurements:
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


def _get_infection_data(person: Person) -> dict:
    data = {}

    measurements = Measurement.objects.filter(person=person)

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

    hiv_measurements = measurements.filter(
        measurement_concept__concept_code__in=['5221-7', '7917-8']
    ).union(
        measurements.filter(measurement_source_value__in=['5221-7', '7917-8'])
    )
    for m in hiv_measurements:
        result = _infection_value(m)
        if result == 'negative':
            data['no_hiv_status'] = True
            data['hiv_status'] = False
        elif result == 'positive':
            data['no_hiv_status'] = False
            data['hiv_status'] = True

    hepb_measurements = measurements.filter(
        measurement_concept__concept_code__in=['5195-3']
    ).union(
        measurements.filter(measurement_source_value__in=['5195-3'])
    )
    for m in hepb_measurements:
        result = _infection_value(m)
        if result == 'negative':
            data['no_hepatitis_b_status'] = True
            data['hepatitis_b_status'] = False
        elif result == 'positive':
            data['no_hepatitis_b_status'] = False
            data['hepatitis_b_status'] = True

    hepc_measurements = measurements.filter(
        measurement_concept__concept_code__in=['5196-1']
    ).union(
        measurements.filter(measurement_source_value__in=['5196-1'])
    )
    for m in hepc_measurements:
        result = _infection_value(m)
        if result == 'negative':
            data['no_hepatitis_c_status'] = True
            data['hepatitis_c_status'] = False
        elif result == 'positive':
            data['no_hepatitis_c_status'] = False
            data['hepatitis_c_status'] = True

    return data


def _get_mm_specific_data(person: Person) -> dict:
    """Derive MM-specific boolean and coded fields from OMOP Observation table.

    Covers fields not derivable via _LOINC_LAB_FIELDS (boolean / string-valued
    observations): plasma_cell_leukemia, bone_lesions, meets_crab.
    Uses measurement_source_value as the LOINC lookup since Concept rows may not
    be loaded in all environments.
    """
    data = {}

    # Query Measurement first (upload handler routes all FHIR Obs through Measurement
    # when they have a recognisable LOINC code).
    MM_LOINC_CODES = {
        '47082-2': 'plasma_cell_leukemia',   # Plasma cell leukemia status (boolean)
        '24646-7': 'bone_lesions',            # Bone lesion presence (boolean → text)
        '89599-5': 'meets_crab',              # CRAB criteria met (boolean)
    }

    mm_measurements = (
        Measurement.objects
        .filter(person=person,
                measurement_source_value__in=list(MM_LOINC_CODES.keys()))
        .order_by('measurement_date')
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
    mm_obs = (
        Observation.objects
        .filter(person=person,
                observation_source_value__in=list(MM_LOINC_CODES.keys()))
        .order_by('observation_date')
    )
    for o in mm_obs:
        field = MM_LOINC_CODES.get(o.observation_source_value)
        _set_mm_field(field, _coerce_mm_boolean(o.value_as_number, o.value_as_string))

    # ── meets_slim: derived from plasma cells and FLC ratio in OMOP ─────────
    # SLiM = Sixty (plasma cells ≥60%), Light chain ratio ≥100, or MRI lesions
    # (MRI lesions are not tracked in OMOP, so we check plasma cells and FLC)
    slim_rows = (
        Measurement.objects
        .filter(
            person=person,
            measurement_source_value__in=['26098-4', '33944-8', '33945-5'],
            value_as_number__isnull=False,
        )
        .values('measurement_source_value', 'value_as_number')
    )
    slim_vals = {}
    for row in slim_rows:
        slim_vals.setdefault(row['measurement_source_value'], []).append(
            float(row['value_as_number'])
        )

    plasma_pcts = slim_vals.get('26098-4', [])
    kappas = slim_vals.get('33944-8', [])
    lambdas = slim_vals.get('33945-5', [])

    meets_slim = False
    # Sixty criterion: any plasma cells measurement ≥60%
    if any(v >= 60.0 for v in plasma_pcts):
        meets_slim = True
    # Light-chain ratio criterion: ratio ≥100 in either direction
    elif kappas and lambdas:
        k = kappas[-1]
        lam = lambdas[-1]
        if lam > 0 and k / lam >= 100:
            meets_slim = True
        elif k > 0 and lam / k >= 100:
            meets_slim = True

    data['meets_slim'] = meets_slim

    # ── meets_crab fallback: compute from OMOP Measurements when obs missing ─
    if 'meets_crab' not in data:
        crab_rows = (
            Measurement.objects
            .filter(
                person=person,
                measurement_source_value__in=['718-7', '59260-0', '17861-6', '2000-0', '2164-2', '33914-3'],
                value_as_number__isnull=False,
            )
            .values('measurement_source_value', 'value_as_number')
        )
        crab_vals = {}
        for row in crab_rows:
            crab_vals.setdefault(row['measurement_source_value'], []).append(float(row['value_as_number']))

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


def _get_sct_cytogenetic_data(person: Person) -> dict:
    """Derive cytogenetic markers and SCT fields from OMOP Observation rows.

    These values are written as Observation rows by the bulk FHIR import
    (and eventually the upload handler) with custom observation_source_value
    keys: mm-cytogenetic-markers, mm-sct-date, mm-sct-history, mm-sct-eligibility.
    """
    data = {}
    _SOURCE_KEYS = [
        'mm-cytogenetic-markers', 'mm-sct-date',
        'mm-sct-history', 'mm-sct-eligibility',
    ]
    obs_qs = (
        Observation.objects.filter(
            person=person,
            observation_source_value__in=_SOURCE_KEYS,
        )
        .order_by('-observation_date')
    )
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


def _get_assessment_data(person: Person) -> dict:
    data = {}

    observations = (
        Observation.objects
        .filter(person=person)
        .select_related('observation_concept')
        .order_by('-observation_date')
    )

    tumor_stage_obs = Observation.objects.filter(
        person=person,
        observation_concept__concept_code__in=['21905-5'],
    ).order_by('-observation_date').first()
    metastasis_obs = Observation.objects.filter(
        person=person,
        observation_concept__concept_code__in=['21901-4'],
    ).order_by('-observation_date').first()

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


def _get_laboratory_data(person: Person) -> dict:
    data = {}

    measurements = (
        Measurement.objects.filter(person=person)
        .select_related('measurement_concept')
        .order_by('-measurement_date')
    )

    # --- Legacy fields via concept-name matching ---
    legacy_lab_mappings = {
        'hemoglobin': ('hemoglobin_level', 'G/DL'),
        'platelet': ('platelet_count', 'CELLS/UL'),
        'creatinine': ('serum_creatinine_level', 'MG/DL'),
        'calcium': ('serum_calcium_level', 'MG/DL'),
        'bilirubin': ('serum_bilirubin_level_total', 'MG/DL'),
        'albumin': ('albumin_level', 'G/DL'),
    }
    for measurement in measurements:
        if not measurement.measurement_concept:
            continue
        if (
            measurement.measurement_source_value
            and measurement.measurement_concept.concept_code
            and measurement.measurement_source_value != measurement.measurement_concept.concept_code
        ):
            continue
        concept_name = measurement.measurement_concept.concept_name.lower()
        for lab_key, (field_name, unit_field) in legacy_lab_mappings.items():
            if field_name in data:
                continue
            if lab_key in concept_name and measurement.value_as_number:
                data[field_name] = measurement.value_as_number
                data[f'{field_name}_units'] = unit_field
                break

    # --- New UI fields via LOINC concept code (primary path) ---
    loinc_ms = measurements.filter(
        value_as_number__isnull=False,
    ).select_related('measurement_concept')
    for m in loinc_ms:
        code = _measurement_code(m)
        if code not in _LOINC_LAB_FIELDS:
            continue
        field, cast = _LOINC_LAB_FIELDS[code]
        if field not in data:
            data[field] = cast(m.value_as_number)

    # --- New UI fields via display-name source_value (legacy/generator path) ---
    unfound = {f for (f, _) in _LOINC_LAB_FIELDS.values() if f not in data}
    if unfound:
        sv_ms = measurements.filter(
            measurement_source_value__in=_SOURCE_VALUE_LAB_FIELDS.keys(),
            value_as_number__isnull=False,
        )
        for m in sv_ms:
            field = _SOURCE_VALUE_LAB_FIELDS.get(m.measurement_source_value)
            if field and field not in data:
                data[field] = float(m.value_as_number)

    return data


def _get_performance_data(person: Person) -> dict:
    data = {}

    observations = (
        Observation.objects.filter(person=person)
        .select_related('observation_concept')
        .order_by('-observation_date', '-observation_id')
    )

    ecog = (
        observations
        .filter(observation_concept__concept_name__icontains='ecog')
        .exclude(value_as_number__isnull=True)
        .first()
    )
    if ecog:
        data['ecog_performance_status'] = int(ecog.value_as_number)
        if ecog.observation_date:
            data['ecog_assessment_date'] = ecog.observation_date

    karnofsky = (
        observations
        .filter(observation_concept__concept_name__icontains='karnofsky')
        .exclude(value_as_number__isnull=True)
        .first()
    )
    if karnofsky:
        data['karnofsky_performance_score'] = int(karnofsky.value_as_number)

    return data


def _get_genetic_mutations(person: Person) -> dict:
    data = {}

    origin_concepts = {255395001: 'germline', 255461003: 'somatic'}
    interpretation_concepts = {30166007: 'pathogenic', 10828004: 'benign', 42425007: 'vus'}

    mutations = []

    genetic_measurements = Measurement.objects.filter(
        person=person,
    ).filter(
        models.Q(measurement_concept__concept_code__in=_GENETIC_MUTATION_LOINCS.keys())
        | models.Q(measurement_source_value__in=_GENETIC_MUTATION_LOINCS.keys())
    ).order_by('-measurement_date')

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


def _get_cll_data(person: Person) -> dict:
    data = {}
    measurements = (
        Measurement.objects
        .filter(person=person)
        .select_related('measurement_concept')
        .order_by('-measurement_date')
    )
    observations = (
        Observation.objects
        .filter(person=person)
        .select_related('observation_concept')
        .order_by('-observation_date')
    )
    conditions = ConditionOccurrence.objects.filter(person=person)

    loinc_map = {
        '731-0':   'absolute_lymphocyte_count',
        '48094-6': 'serum_beta2_microglobulin_level',
        '8632-1':  'qtcf_value',
        '44996-6': 'spleen_size',
        '21889-1': 'largest_lymph_node_size',
    }
    for loinc_code, field in loinc_map.items():
        m = measurements.filter(
            measurement_concept__concept_code=loinc_code,
            value_as_number__isnull=False,
        ).first()
        if m:
            data[field] = float(m.value_as_number)

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

    drug_exposures = DrugExposure.objects.filter(person=person)
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

    has_progression = observations.filter(
        observation_concept__concept_code='182842009'
    ).exists()

    if had_btk:
        data['btk_inhibitor_refractory'] = has_progression
    if had_bcl2:
        data['bcl2_inhibitor_refractory'] = has_progression

    alc_loinc = '731-0'
    alc_concept = Concept.objects.filter(
        concept_code=alc_loinc,
        vocabulary__vocabulary_id='LOINC',
    ).first()
    if alc_concept:
        alc_measurements = (
            Measurement.objects.filter(
                person=person,
                measurement_concept=alc_concept,
                value_as_number__isnull=False,
            ).order_by('measurement_date')
        )
        if alc_measurements.count() >= 2:
            pts = list(alc_measurements.values_list('measurement_date', 'value_as_number'))
            ldt = _compute_lymphocyte_doubling_time(pts)
            if ldt is not None:
                data['lymphocyte_doubling_time'] = ldt

    return data


def _get_lymphoma_data(person: Person) -> dict:
    data = {}
    observations = (
        Observation.objects
        .filter(person=person)
        .select_related('observation_concept')
        .order_by('-observation_date')
    )
    measurements = (
        Measurement.objects
        .filter(person=person)
        .select_related('measurement_concept')
        .order_by('-measurement_date')
    )

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

    data.update(_get_dlbcl_transformation(person, observations))

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


def _get_dlbcl_transformation(person: Person, observations) -> dict:
    """Derive FL → DLBCL transformation fields from OMOP.

    Evidence, in order:
      1. a DLBCL ConditionOccurrence (transformation date = its start date)
      2. fallback: an Observation whose concept name or source value mentions
         histologic transformation (transformation date = observation date)

    Post-transformation outcome precedence: death on/after transformation →
    'Deceased'; else the latest post-transformation LOT-line outcome
    Observation; else None (manual entry).

    `observations` is the person's Observation queryset ordered by
    -observation_date (already fetched by _get_lymphoma_data).
    """
    data = {}

    transformation_date = None
    dlbcl_conditions = (
        ConditionOccurrence.objects
        .filter(person=person)
        .select_related('condition_concept')
        .order_by('condition_start_date')
    )
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

    death = Death.objects.filter(person=person).only('death_date').first()
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


def _get_prior_procedures(person: Person) -> dict:
    """Extract ProcedureOccurrence records into prior_procedures JSONField."""
    data = {}

    procedures = ProcedureOccurrence.objects.filter(
        person=person,
    ).select_related('procedure_concept').order_by('-procedure_date')

    if procedures.exists():
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

    alc = patient_info.absolute_lymphocyte_count
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


def _get_wearable_data(person: Person) -> dict:
    """Derive 30-day wearable summaries from OMOP Measurement/Observation rows."""
    data = {}

    measurement_rows = list(
        Measurement.objects.filter(
            person=person,
            value_as_number__isnull=False,
        )
        .filter(
            models.Q(measurement_concept__concept_code__in=WEARABLE_LOINC.values())
            | models.Q(measurement_source_value__in=WEARABLE_LOINC.values())
        )
        .values_list(
            'measurement_concept__concept_code',
            'measurement_source_value',
            'measurement_date',
            'value_as_number',
        )
    )
    observation_rows = list(
        Observation.objects.filter(
            person=person,
            value_as_number__isnull=False,
        )
        .filter(
            models.Q(observation_concept__concept_code=WEARABLE_LOINC['sleep_duration'])
            | models.Q(observation_source_value=WEARABLE_LOINC['sleep_duration'])
        )
        .values_list('observation_date', 'value_as_number')
    )

    if not measurement_rows and not observation_rows:
        return data

    # Key each row by the wearable LOINC it actually carries. A row may have an
    # unrelated concept_code (e.g. an unmapped source concept) yet a wearable
    # measurement_source_value — prefer whichever is a known wearable code so
    # the source_value fallback isn't shadowed by a present-but-unrelated concept.
    _wearable_codes = set(WEARABLE_LOINC.values())
    rows_by_code: dict[str, list[tuple[date, float]]] = {}
    for concept_code, source_value, mdate, val in measurement_rows:
        code = concept_code if concept_code in _wearable_codes else source_value
        if code not in _wearable_codes:
            continue
        rows_by_code.setdefault(code, []).append((mdate, float(val)))

    def _metric_daily(metric_key):
        loinc_code = WEARABLE_LOINC[metric_key]
        lo, hi = WEARABLE_ARTIFACT_BOUNDS[metric_key]
        daily: dict[date, list[float]] = {}
        for mdate, val in rows_by_code.get(loinc_code, []):
            if lo <= val <= hi:
                daily.setdefault(mdate, []).append(val)
        return daily

    steps_daily = _metric_daily('steps')
    active_daily = _metric_daily('active_minutes')
    rhr_daily = _metric_daily('resting_hr')
    hrv_daily = _metric_daily('hrv_sdnn')
    spo2_daily = _metric_daily('spo2')
    rr_daily = _metric_daily('respiratory_rate')

    sleep_daily: dict[date, list[float]] = {}
    lo_s, hi_s = WEARABLE_ARTIFACT_BOUNDS['sleep_duration']
    for odate, val in observation_rows:
        fval = float(val)
        if lo_s <= fval <= hi_s:
            sleep_daily.setdefault(odate, []).append(fval)
    for mdate, val in rows_by_code.get(WEARABLE_LOINC['sleep_duration'], []):
        if lo_s <= val <= hi_s:
            sleep_daily.setdefault(mdate, []).append(val)

    all_valid_days = sorted(
        steps_daily.keys() | active_daily.keys()
        | rhr_daily.keys() | hrv_daily.keys()
        | spo2_daily.keys() | rr_daily.keys()
        | sleep_daily.keys()
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
    hrv_daily = _within_window(hrv_daily)
    spo2_daily = _within_window(spo2_daily)
    rr_daily = _within_window(rr_daily)
    sleep_daily = _within_window(sleep_daily)

    steps_totals = {d: sum(vs) for d, vs in steps_daily.items()}
    active_totals = {d: sum(vs) for d, vs in active_daily.items()}
    sleep_nightly = {d: sum(vs) for d, vs in sleep_daily.items()}

    # ---- Coverage ratio (union of all wearable metric days) ----------
    all_valid_days = (
        steps_totals.keys() | active_totals.keys()
        | rhr_daily.keys() | hrv_daily.keys()
        | spo2_daily.keys() | rr_daily.keys()
        | sleep_nightly.keys()
    )
    coverage = round(len(all_valid_days) / 30.0, 2)
    data['wearable_coverage_ratio_30d'] = coverage

    # ---- Median daily steps ------------------------------------------
    if len(steps_totals) >= WEARABLE_MIN_VALID_DAYS:
        data['median_daily_steps_30d'] = int(statistics.median(steps_totals.values()))

    # ---- Active minutes ----------------------------------------------
    if len(active_totals) >= WEARABLE_MIN_VALID_DAYS:
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
    if len(rhr_means) >= WEARABLE_MIN_VALID_DAYS:
        data['resting_heart_rate_avg_30d'] = int(round(statistics.mean(rhr_means.values())))

    # ---- HRV SDNN ----------------------------------------------------
    hrv_means = {d: statistics.mean(vs) for d, vs in hrv_daily.items()}
    if len(hrv_means) >= WEARABLE_MIN_VALID_DAYS:
        data['hrv_sdnn_avg_30d'] = round(statistics.mean(hrv_means.values()), 1)

    # ---- SpO2 minimum (single low reading is clinically significant) -
    all_spo2 = [v for vs in spo2_daily.values() for v in vs]
    if all_spo2:
        data['oxygen_saturation_min_30d'] = round(min(all_spo2), 2)

    # ---- Respiratory rate -------------------------------------------
    rr_means = {d: statistics.mean(vs) for d, vs in rr_daily.items()}
    if len(rr_means) >= WEARABLE_MIN_VALID_DAYS:
        data['respiratory_rate_avg_30d'] = round(statistics.mean(rr_means.values()), 1)

    # ---- Sleep duration (from Observation) ---------------------------
    if len(sleep_nightly) >= WEARABLE_MIN_VALID_DAYS:
        data['sleep_duration_hours_avg_30d'] = round(
            statistics.mean(sleep_nightly.values()), 1
        )

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
