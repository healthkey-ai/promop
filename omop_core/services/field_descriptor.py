"""
field_descriptor.py — Classify every PatientRecord field by its mapping status.

Used by the concept-mapping admin interface to show which fields have OMOP
concept assignments and which still need one.
"""
from __future__ import annotations

from omop_core.models import CustomPatientField, PatientRecord, FieldConceptMapping, FieldChoice, FieldFormula
from omop_core.services.mappings import (
    LAB_FIELD_TO_LOINC,
    LAB_FIELD_ALIAS_TO_CANONICAL,
    DEMOGRAPHIC_FIELDS,
    THERAPY_LINE_FIELDS,
    DERIVED_FIELD_TO_CODE,
    FIELD_COMMON_UNITS,
    STANDARD_UNIT_CHOICES,
    SUGGESTED_FIELD_CODES,
)
from omop_core.services.patient_record_service import (
    PATIENT_RECORD_OMOP_MAPPED_FIELDS,
    _LAB_FIELD_ALIASES,
    _LOINC_LAB_FIELDS,
)
from omop_core.services.provenance_registry import get_registry
from omop_core.services.formula_evaluator import validate_formula


# Fields that are purely internal / structural and not clinical.
_INTERNAL_FIELDS = frozenset({
    'id', 'person', 'organization', 'created_at', 'updated_at',
    'derivation_version', 'derived_at', 'user_edited_fields',
    # Redundant generic therapy fields — duplicated by per-line fields.
    'therapy_intent', 'reason_for_discontinuation',
    # Renamed field — replaced by remission_duration.
    'remission_duration_min',
})

# Person/profile fields projected from Person model.
_PERSON_FIELDS = frozenset({
    'date_of_birth', 'gender', 'race', 'ethnicity', 'languages_skills',
    'email', 'phone_number', 'facility_name', 'validated', 'validated_by',
    'validation_date', 'suppress_demographics_for_others', 'patient_age',
})

# Location fields.
_LOCATION_FIELDS = frozenset({
    'country', 'region', 'city', 'postal_code', 'latitude', 'longitude',
})

# Wearable 30-day summary fields.
_WEARABLE_30D_SUFFIX = '_30d'
_WEARABLE_METADATA_FIELDS = frozenset({
    'wearable_last_sync_at', 'wearable_coverage_ratio_30d',
})

# Treatment fields that curators can directly edit (not computed).
# Therapy line fields from THERAPY_LINE_FIELDS are editable, plus supportive
# therapy, concomitant medication, and toxicity fields.
_EDITABLE_TREATMENT_FIELDS = frozenset({
    # Supportive therapy
    'supportive_therapies', 'supportive_therapy_start_date',
    'supportive_therapy_end_date', 'supportive_therapy_intent',
    'supportive_therapy_date',
    # Concomitant medication (editable; no_concomitant_medication_status is computed)
    'concomitant_medication_date', 'concomitant_medications',
    'concomitant_medication_details',
    # Other editable treatment fields
    'toxicity_grade', 'planned_therapies', 'concomitant_medication',
    'remission_duration',
})

# Computed therapy-related fields (IDs, counts, summaries — not directly editable).
_COMPUTED_THERAPY_FIELDS = frozenset({
    # Per-line IDs
    'first_line_therapy_id', 'first_line_component_ids', 'first_line_therapy_type_ids',
    'second_line_therapy_id', 'second_line_component_ids', 'second_line_therapy_type_ids',
    'later_therapy_ids', 'later_component_ids', 'later_therapy_type_ids',
    'therapy_component_ids', 'therapy_type_ids',
    # Summaries and provenance
    'therapy_ids_provenance', 'therapy_lines_count', 'last_treatment',
    'treatment_refractory_status', 'relapse_count', 'refractory_status',
    'washout_period_duration', 'prior_therapy', 'line_of_therapy',
    'later_therapies', 'later_date',
    # Negation field
    'no_concomitant_medication_status',
})

# Computed fields (derived from other fields, not directly from OMOP).
_COMPUTED_FIELDS = frozenset({
    'bmi', 'disease_slug',
    'meets_crab', 'meets_slim', 'involved_uninvolved_ratio',
    'active_infection_status', 'active_malignancies',
    'no_active_infection_status', 'no_hiv_status', 'no_hepatitis_b_status',
    'no_hepatitis_c_status', 'no_other_active_malignancies',
    'no_pre_existing_conditions', 'no_pregnancy_or_lactation_status',
    'no_mental_health_disorder_status',
    'no_tobacco_use_status', 'no_substance_use_status',
    'no_geographic_exposure_risk',
})

# Unit-companion fields (always paired with a measurement field). Units are
# curated on the corresponding measurement mapping, so these implementation
# columns do not belong in the Field Concept Mapping inventory.
_UNIT_SUFFIX = '_units'

# Specific explanations for computed therapy fields.
_COMPUTED_THERAPY_EXPLANATIONS = {
    'therapy_lines_count': 'Count of therapy line Episode records',
    'last_treatment': 'Latest therapy end date across all lines',
    'prior_therapy': 'Derived from therapy_lines_count',
    'treatment_refractory_status': 'Computed from therapy line outcomes',
    'relapse_count': 'Computed from therapy line outcomes',
    'refractory_status': 'Computed from therapy line outcomes',
    'washout_period_duration': 'Computed from last therapy received',
    'line_of_therapy': 'Derived from therapy line Episode records',
    'no_concomitant_medication_status': 'Computed negation of concomitant medication presence',
    'later_therapies': 'Derived from Episode and DrugExposure records',
    'later_date': 'Derived from Episode and DrugExposure records',
}


def _get_explanation(field_name: str, category: str) -> str | None:
    """Return a human-readable explanation for a computed field without a formula."""
    if category != 'computed':
        return None
    if field_name in _COMPUTED_THERAPY_FIELDS:
        return _COMPUTED_THERAPY_EXPLANATIONS.get(
            field_name, 'Derived from Episode and DrugExposure records',
        )
    if field_name in _COMPUTED_FIELDS:
        explanations = {
            'bmi': 'Calculated from weight and height',
            'disease_slug': 'URL-safe slug derived from disease name',
            'meets_crab': 'Derived from CRAB criteria fields',
            'meets_slim': 'Derived from SLiM criteria fields',
            'involved_uninvolved_ratio': 'Calculated from kappa and lambda FLC values',
        }
        return explanations.get(field_name)
    if field_name.endswith(_WEARABLE_30D_SUFFIX) or field_name in _WEARABLE_METADATA_FIELDS:
        return 'Aggregated from wearable device data (30-day window)'
    return None


# Build the set of all alias field names.
_ALL_ALIASES = set(LAB_FIELD_ALIAS_TO_CANONICAL.keys())
for aliases in _LAB_FIELD_ALIASES.values():
    _ALL_ALIASES.update(aliases)

# Fields that are editable via the LOINC lab write-through: either in
# LAB_FIELD_TO_LOINC (the write map) or as a target in _LOINC_LAB_FIELDS
# (the derivation map, which names fields the extractor can populate via a
# known LOINC code — those 18 recovered attributions from #596/#607).
_LOINC_EDITABLE_FIELDS = frozenset(LAB_FIELD_TO_LOINC.keys()) | frozenset(
    field_name for field_name, _cast in _LOINC_LAB_FIELDS.values()
)


def _get_field_type_label(field) -> str:
    """Return a human-readable type label for a Django model field."""
    type_map = {
        'CharField': 'text',
        'TextField': 'text',
        'IntegerField': 'integer',
        'FloatField': 'float',
        'DecimalField': 'decimal',
        'BooleanField': 'boolean',
        'NullBooleanField': 'boolean',
        'DateField': 'date',
        'DateTimeField': 'datetime',
        'JSONField': 'json',
        'ForeignKey': 'fk',
        'BigIntegerField': 'integer',
        'SmallIntegerField': 'integer',
        'PositiveIntegerField': 'integer',
    }
    class_name = field.__class__.__name__
    return type_map.get(class_name, class_name.lower())


# ── Tab classification ─────────────────────────────────────────────
# Maps each field to the clinical tab it appears on in the patient detail view.

_TAB_GENERAL = frozenset({
    'date_of_birth', 'gender', 'race', 'ethnicity', 'email', 'phone_number',
    'country', 'region', 'city', 'postal_code', 'latitude', 'longitude',
    'disease', 'stage', 'histologic_type',
    'ecog_performance_status', 'ecog_assessment_date', 'karnofsky_performance_score',
    'preexisting_conditions', 'peripheral_neuropathy_grade',
    'no_other_active_malignancies', 'no_active_infection_status',
    'hiv_status', 'no_hiv_status', 'hepatitis_b_status', 'no_hepatitis_b_status',
    'hepatitis_c_status', 'no_hepatitis_c_status',
    'weight', 'height', 'bmi', 'systolic_blood_pressure', 'diastolic_blood_pressure',
    'heartrate', 'languages_skills', 'facility_name',
    'validated', 'validated_by', 'validation_date', 'patient_age',
    'suppress_demographics_for_others',
    # Reclassified from "other"
    'diagnosis_date', 'death_date', 'heartrate_variability',
    'no_pre_existing_conditions', 'ejection_fraction',
})

_TAB_DISEASE = frozenset({
    # Breast cancer
    'menopausal_status', 'tumor_stage', 'nodes_stage', 'staging_modalities',
    'distant_metastasis_stage', 'bone_only_metastasis_status',
    'measurable_disease_by_recist_status',
    'estrogen_receptor_status', 'progesterone_receptor_status',
    'her2_status', 'hr_status', 'hrd_status', 'androgen_receptor_status',
    'tnbc_status', 'ki67_proliferation_index', 'pd_l1_tumor_cells',
    'oncotype_dx_score', 'test_methodology', 'test_date', 'test_specimen_type',
    'report_interpretation', 'genetic_mutations',
    # Lymphoma
    'tumor_grade', 'gelf_criteria_status', 'flipi_score', 'flipi_risk_category',
    'flipi_score_options', 'bulky_disease', 'b_symptoms',
    'transformed_to_dlbcl', 'dlbcl_transformation_date', 'post_transformation_outcome',
    'bone_marrow_involvement', 'number_of_nodal_sites',
    'clonal_bone_marrow_b_lymphocytes',
    # Myeloma
    'myeloma_type', 'r_iss_stage', 'durie_salmon_stage', 'progression',
    'measurable_disease_imwg', 'mrd_status', 'meets_crab', 'meets_slim',
    'stem_cell_transplant_history', 'sct_date', 'sct_eligibility',
    'monoclonal_protein_serum', 'monoclonal_protein_urine',
    'kappa_flc', 'lambda_flc', 'kappa_lambda_ratio', 'involved_uninvolved_ratio',
    'bone_lesions', 'hypercalcemia', 'renal_impairment', 'anemia',
    'clonal_plasma_cells', 'cytogenetic_risk', 'cytogenetic_abnormalities',
    # CLL
    'binet_stage', 'tumor_burden', 'disease_activity', 'richter_transformation',
    'protein_expressions', 'absolute_lymphocyte_count', 'lymphocyte_doubling_time',
    'serum_beta2_microglobulin_level', 'clonal_b_lymphocyte_count',
    'qtcf_value', 'largest_lymph_node_size', 'spleen_size',
    'tp53_disruption', 'measurable_disease_iwcll', 'splenomegaly', 'hepatomegaly',
    'lymphadenopathy', 'autoimmune_cytopenias_refractory_to_steroids',
    'btk_inhibitor_refractory', 'bcl2_inhibitor_refractory',
    # Shared disease markers
    'ldh_level', 'beta2_microglobulin', 'disease_slug',
    # Reclassified from "other"
    'tumor_size', 'lymph_node_status', 'metastasis_status',
    'biopsy_grade', 'biopsy_grade_depr', 'plasma_cell_leukemia',
    'pd_l1_assay', 'pd_l1_ic_percentage', 'pd_l1_combined_positive_score',
    'cytogenic_markers', 'molecular_markers',
    'condition_code_icd_10', 'condition_code_snomed_ct',
    'condition_clinical_status', 'prior_procedures',
    'metastatic_status', 'active_infection_status', 'active_malignancies',
    'no_active_infection_status', 'no_other_active_malignancies',
})

_TAB_TREATMENT = frozenset({
    'therapy_lines_count', 'relapse_count', 'refractory_status',
    'first_line_therapy', 'first_line_start_date', 'first_line_end_date',
    'first_line_intent', 'first_line_discontinuation_reason', 'first_line_outcome',
    'first_line_component_ids', 'first_line_therapy_type_ids',
    'second_line_therapy', 'second_line_start_date', 'second_line_end_date',
    'second_line_intent', 'second_line_discontinuation_reason', 'second_line_outcome',
    'second_line_component_ids', 'second_line_therapy_type_ids',
    'later_therapy', 'later_start_date', 'later_end_date',
    'later_intent', 'later_discontinuation_reason', 'later_outcome',
    'later_therapies', 'later_component_ids', 'later_therapy_type_ids',
    'supportive_therapy_start_date', 'supportive_therapy_end_date',
    'supportive_therapies', 'supportive_therapy_intent',
    'planned_therapies', 'concomitant_medication',
    # Reclassified from "other"
    'toxicity_grade', 'concomitant_medications', 'concomitant_medication_date',
    'concomitant_medication_details', 'washout_period_duration',
    'remission_duration',
    'no_concomitant_medication_status', 'later_date',
    'treatment_refractory_status', 'therapy_ids_provenance',
    'last_treatment', 'prior_therapy', 'line_of_therapy',
})

_TAB_BLOOD = frozenset({
    'hemoglobin_g_dl', 'hematocrit_percent', 'wbc_count_thousand_per_ul',
    'rbc_million_per_ul', 'platelet_count_thousand_per_ul',
    'anc_thousand_per_ul', 'alc_thousand_per_ul', 'amc_thousand_per_ul',
    'sodium_meq_l', 'potassium_meq_l', 'calcium_mg_dl', 'magnesium_mg_dl',
    'troponin_ng_ml', 'bnp_pg_ml', 'glucose_mg_dl', 'hba1c_percent', 'ldh_u_l',
    'inr', 'pt_seconds', 'ptt_seconds',
    'cea_ng_ml', 'ca19_9_u_ml', 'psa_ng_ml',
    # Legacy aliases for blood counts
    'hemoglobin_level', 'platelet_count', 'white_blood_cell_count',
})

_TAB_LABS = frozenset({
    'serum_creatinine_level', 'creatinine_clearance_rate', 'blood_urea_nitrogen',
    'egfr', 'serum_sodium', 'serum_potassium', 'serum_calcium_level',
    'magnesium', 'phosphorus', 'albumin_level', 'total_protein',
    'liver_enzyme_levels_ast', 'liver_enzyme_levels_alt', 'liver_enzyme_levels_alp',
    'serum_bilirubin_level_total', 'serum_bilirubin_level_direct', 'albumin_g_dl',
    'ldh', 'alkaline_phosphatase', 'c_reactive_protein', 'esr',
    'pulmonary_function_test_result', 'bone_imaging_result',
    # Additional laboratory and organ-function measurements.
    'alkaline_phosphatase_u_l', 'alt_u_l', 'ast_u_l', 'bilirubin_total_mg_dl',
    'bun_mg_dl', 'creatinine_clearance_ml_min', 'creatinine_mg_dl',
    'egfr_ml_min_173m2', 'estimated_glomerular_filtration_rate',
    'lactate_dehydrogenase_level', 'renal_adequacy_status',
    'serum_calcium_mg_dl', 'serum_creatinine_mg_dl',
    'liver_enzyme_levels',
})

_TAB_BEHAVIOR = frozenset({
    'smoking_status', 'pack_years', 'alcohol_use', 'drinks_per_week',
    'exercise_frequency', 'exercise_minutes_per_week', 'diet_type',
    'sleep_hours_per_night', 'sleep_quality', 'stress_level', 'social_support',
    'employment_status', 'education_level', 'marital_status', 'insurance_type',
    'number_of_dependents', 'annual_household_income',
    'pregnancy_test_date', 'pregnancy_test_result_value', 'contraceptive_use',
    'consent_capability', 'caregiver_availability_status',
    'no_mental_health_disorder_status', 'no_substance_use_status',
    'substance_use_details', 'no_geographic_exposure_risk',
    'geographic_exposure_risk_details',
    # Reproductive results and wearable lifestyle summaries are best reviewed
    # alongside the other behavior and eligibility information.
    'pregnancy_test_result', 'no_pregnancy_or_lactation_status',
    'no_tobacco_use_status', 'tobacco_use_details',
    'wearable_last_sync_at', 'wearable_coverage_ratio_30d',
    'median_daily_steps_30d', 'active_minutes_per_day_30d', 'activity_trend_30d',
    'resting_heart_rate_avg_30d', 'hrv_sdnn_avg_30d', 'hrv_rmssd_avg_30d',
    'oxygen_saturation_min_30d', 'oxygen_saturation_avg_30d', 'respiratory_rate_avg_30d',
    'sleep_duration_hours_avg_30d', 'vo2_max_avg_30d',
    'distance_km_per_day_30d', 'walking_speed_avg_30d', 'walking_step_length_avg_30d',
    'walking_double_support_pct_avg_30d', 'walking_hr_avg_30d',
    'flights_climbed_per_day_30d', 'active_energy_per_day_30d',
    'basal_energy_per_day_30d', 'body_mass_avg_30d',
})


def _classify_tab(field_name: str) -> str:
    """Return the clinical-tab label for a PatientRecord field.

    Internal fields are excluded from the API entirely (no tab).
    Wearable _30d fields are computed — they go to 'other' and render
    in the Computed section at the bottom of each tab.
    """
    if field_name in _TAB_GENERAL:
        return 'general'
    if field_name in _TAB_DISEASE:
        return 'disease'
    if field_name in _TAB_TREATMENT:
        return 'treatment'
    if field_name in _TAB_BLOOD:
        return 'blood'
    if field_name in _TAB_LABS:
        return 'labs'
    if field_name in _TAB_BEHAVIOR:
        return 'behavior'
    # Therapy-related fallback.
    therapy_keywords = (
        'therapy', 'treatment', 'line_', 'component_ids', 'therapy_type_ids',
    )
    if any(kw in field_name for kw in therapy_keywords):
        return 'treatment'
    return 'other'


def _classify_field(field_name: str) -> str:
    """Assign a category to a PatientRecord field."""
    if field_name in _INTERNAL_FIELDS:
        return 'internal'
    if field_name in _PERSON_FIELDS:
        return 'profile'
    if field_name in _LOCATION_FIELDS:
        return 'location'
    if field_name in _LOINC_EDITABLE_FIELDS:
        return 'editable'
    if field_name in _ALL_ALIASES:
        return 'alias'
    if field_name.endswith(_UNIT_SUFFIX):
        return 'unit'
    if field_name in DEMOGRAPHIC_FIELDS:
        return 'profile'
    # Therapy line fields (names, dates, outcomes, intents, reasons) are editable.
    if field_name in THERAPY_LINE_FIELDS:
        return 'editable'
    # Additional editable treatment fields (supportive therapy, concomitant meds, toxicity).
    if field_name in _EDITABLE_TREATMENT_FIELDS:
        return 'editable'
    # Computed therapy fields (IDs, counts, summaries).
    if field_name in _COMPUTED_THERAPY_FIELDS:
        return 'computed'
    if field_name in _COMPUTED_FIELDS:
        return 'computed'
    if field_name in _WEARABLE_METADATA_FIELDS:
        return 'computed'
    if field_name.endswith(_WEARABLE_30D_SUFFIX):
        return 'computed'
    if field_name in PATIENT_RECORD_OMOP_MAPPED_FIELDS:
        return 'needs-concept-set'
    return 'other'


_NON_MAPPABLE_CATEGORIES = frozenset({
    'internal', 'computed', 'location', 'alias', 'unit', 'therapy-inference',
})


def _is_mappable(category: str) -> bool:
    """Return True if a field with this category can be concept-mapped."""
    return category not in _NON_MAPPABLE_CATEGORIES


def _get_locked_table(category: str) -> str | None:
    """Return the fixed OMOP table for locked categories, or None."""
    if category == 'profile':
        return 'Person'
    if category == 'location':
        return 'Location'
    return None


def _build_suggestion(name: str, prov_dict: dict | None) -> dict | None:
    """Build auto-suggestion from LAB_FIELD_TO_LOINC, DERIVED_FIELD_TO_CODE, or provenance."""
    common_units = FIELD_COMMON_UNITS.get(name, [])

    if name in LAB_FIELD_TO_LOINC:
        code, unit, display = LAB_FIELD_TO_LOINC[name]
        return {
            'concept_code': code,
            'vocabulary_id': 'LOINC',
            'unit': unit,
            'omop_table': 'Measurement',
            'common_units': common_units,
        }

    if name in DERIVED_FIELD_TO_CODE:
        code, vocab, _ = DERIVED_FIELD_TO_CODE[name]
        omop_table = prov_dict.get('omop_table', '') if prov_dict else ''
        return {
            'concept_code': code,
            'vocabulary_id': vocab,
            'unit': None,
            'omop_table': omop_table,
            'common_units': common_units,
        }

    if prov_dict and prov_dict.get('concept_codes'):
        codes = prov_dict['concept_codes']
        strategy = prov_dict.get('lookup_strategy', '')
        return {
            'concept_code': codes[0],
            'vocabulary_id': strategy.upper() if strategy in ('loinc', 'snomed') else None,
            'unit': None,
            'omop_table': prov_dict.get('omop_table', ''),
            'common_units': common_units,
        }

    # Curator-oriented suggestions — not used by derivation/write-through.
    if name in SUGGESTED_FIELD_CODES:
        code, vocab = SUGGESTED_FIELD_CODES[name]
        omop_table = prov_dict.get('omop_table', 'Observation') if prov_dict else 'Observation'
        return {
            'concept_code': code,
            'vocabulary_id': vocab,
            'unit': None,
            'omop_table': omop_table,
            'common_units': common_units,
        }

    return None


def get_all_field_descriptors() -> list[dict]:
    """Return a descriptor dict for every concrete PatientRecord field.

    Each dict contains:
      - field_name, field_type, category, tab
      - provenance: dict|None (from provenance registry)
      - mapping: dict|None (from FieldConceptMapping table)
      - suggestion: dict|None (auto-suggested concept mapping)
      - mappable: bool (whether the field can be concept-mapped)
      - locked_table: str|None (fixed OMOP table for profile/location)

    Internal fields (id, person, organization, timestamps) are excluded.
    """
    # 1. Get all concrete fields from the model.
    concrete_fields = [
        f for f in PatientRecord._meta.get_fields()
        if getattr(f, 'concrete', False)
    ]

    # 2. Load provenance registry.
    registry = get_registry()

    # 3. Load all existing FieldConceptMapping rows.
    mappings_by_field = {
        m.field_name: m
        for m in FieldConceptMapping.objects.select_related('concept', 'reviewer').all()
    }

    # 3b. Load field choices (curator-managed value sets).
    choices_by_field: dict[str, list[dict]] = {}
    for fc in FieldChoice.objects.prefetch_related('codes').all():
        choices_by_field.setdefault(fc.field_name, []).append({
            'id': fc.id,
            'display': fc.display,
            'sort_order': fc.sort_order,
            'codes': [
                {'code': c.code, 'vocabulary_id': c.vocabulary_id,
                 'display': c.display, 'is_primary': c.is_primary}
                for c in fc.codes.all()
            ],
        })

    # 3c. Load field formulas.
    formulas_by_field = {f.field_name: f for f in FieldFormula.objects.all()}

    # 4. Build descriptors (excluding internal fields).
    result = []
    for f in concrete_fields:
        name = f.name
        category = _classify_field(name)

        # Skip internal and unit-companion fields — neither has an independent
        # OMOP concept mapping to curate.
        if category in {'internal', 'unit'}:
            continue

        # Provenance from registry.
        prov = registry.get(name)
        prov_dict = None
        if prov:
            prov_dict = {
                'omop_table': prov.omop_table,
                'lookup_strategy': prov.lookup_strategy,
                'concept_codes': prov.concept_codes,
                'source_values': prov.source_values,
                'extractor': prov.extractor,
                'selection_rule': prov.selection_rule,
                'description': prov.description,
            }

        # FieldConceptMapping from DB.
        mapping = mappings_by_field.get(name)
        mapping_dict = None
        if mapping:
            mapping_dict = {
                'id': mapping.id,
                'concept_id': mapping.concept_id,
                'concept_name': mapping.concept.concept_name if mapping.concept else '',
                'vocabulary_id': mapping.vocabulary_id,
                'concept_code': mapping.concept_code,
                'unit': mapping.unit,
                'omop_table': mapping.omop_table,
                'status': mapping.status,
                'reviewer': mapping.reviewer.username if mapping.reviewer else None,
                'reviewed_at': mapping.reviewed_at.isoformat() if mapping.reviewed_at else None,
                'notes': mapping.notes,
            }

        formula = formulas_by_field.get(name)
        formula_dict = None
        derivation_error = None
        if formula:
            formula_dict = {
                'id': formula.id,
                'expression': formula.formula,
                'is_active': formula.is_active,
            }
            validation = validate_formula(formula.formula)
            if not validation.valid:
                derivation_error = f"Invalid formula: {'; '.join(validation.errors)}"

        result.append({
            'field_name': name,
            'field_type': _get_field_type_label(f),
            'category': category,
            'tab': _classify_tab(name),
            'provenance': prov_dict,
            'mapping': mapping_dict,
            'suggestion': _build_suggestion(name, prov_dict),
            'unit_options': FIELD_COMMON_UNITS.get(name, STANDARD_UNIT_CHOICES),
            'mappable': _is_mappable(category),
            'locked_table': _get_locked_table(category),
            'choices': choices_by_field.get(name, []),
            'formula': formula_dict,
            'explanation': _get_explanation(name, category),
            # Generic derivation health surface. Other extractors can add a
            # message here without changing the admin API contract.
            'derivation_error': derivation_error,
        })

    # Runtime PatientRecord fields use the JSON projection rather than Django
    # columns, so they are appended explicitly.  This keeps a newly added field
    # discoverable in the mapper immediately after its approved mapping saves.
    for custom in CustomPatientField.objects.select_related('mapping__concept').all():
        mapping = custom.mapping
        formula = formulas_by_field.get(custom.field_name)
        formula_dict = None
        derivation_error = None
        if formula:
            formula_dict = {
                'id': formula.id,
                'expression': formula.formula,
                'is_active': formula.is_active,
            }
            validation = validate_formula(formula.formula)
            if not validation.valid:
                derivation_error = f"Invalid formula: {'; '.join(validation.errors)}"
        result.append({
            'field_name': custom.field_name,
            'field_type': custom.field_type,
            'category': 'computed' if custom.mode == 'computed' else 'needs-concept-set',
            'tab': custom.tab,
            'provenance': None,
            'mapping': {
                'id': mapping.id,
                'concept_id': mapping.concept_id,
                'concept_name': mapping.concept.concept_name if mapping.concept else '',
                'vocabulary_id': mapping.vocabulary_id,
                'concept_code': mapping.concept_code,
                'unit': mapping.unit,
                'omop_table': mapping.omop_table,
                'status': mapping.status,
                'reviewer': mapping.reviewer.username if mapping.reviewer else None,
                'reviewed_at': mapping.reviewed_at.isoformat() if mapping.reviewed_at else None,
                'notes': mapping.notes,
            },
            'suggestion': None,
            'unit_options': STANDARD_UNIT_CHOICES,
            'mappable': True,
            'locked_table': None,
            'choices': choices_by_field.get(custom.field_name, []),
            'formula': formula_dict,
            'explanation': 'Administrator-defined PatientRecord field.',
            'derivation_error': derivation_error,
        })

    # Sort: needs-concept-set first, then by field name.
    category_order = {
        'needs-concept-set': 0,
        'editable': 1,
        'therapy-inference': 2,
        'computed': 3,
        'alias': 4,
        'unit': 5,
        'profile': 6,
        'location': 7,
        'other': 8,
    }
    result.sort(key=lambda d: (category_order.get(d['category'], 99), d['field_name']))
    return result
