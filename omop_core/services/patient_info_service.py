"""
patient_info_service.py — Reusable service for deriving PatientInfo from OMOP tables.

Usage:
    from omop_core.services.patient_info_service import refresh_patient_info
    patient_info = refresh_patient_info(person)
"""

import math
from datetime import date
from django.db import transaction
from omop_core.models import (
    Person, PatientInfo, ConditionOccurrence, Concept,
    Measurement, Observation, DrugExposure, Location, ProcedureOccurrence,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Fields that are entirely derived from OMOP tables and must be reset before
# each refresh so deletions are reflected (not just additions).
_OMOP_DERIVED_FIELDS = [
    # Disease / condition
    'disease', 'diagnosis_date', 'condition_clinical_status', 'disease_slug',
    # Therapy lines
    'first_line_therapy', 'first_line_date', 'first_line_start_date', 'first_line_end_date',
    'second_line_therapy', 'second_line_date', 'second_line_start_date', 'second_line_end_date',
    'later_therapy', 'later_date', 'later_therapies',
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
    # Vitals
    'systolic_blood_pressure', 'diastolic_blood_pressure', 'heartrate',
    'weight', 'weight_units', 'height', 'height_units', 'temperature',
    # Performance
    'ecog_performance_status', 'karnofsky_performance_score',
    # Biomarkers
    'pd_l1_tumor_cells', 'pd_l1_assay',
    'estrogen_receptor_status', 'progesterone_receptor_status', 'her2_status', 'tnbc_status',
    # Genomics
    'genetic_mutations',
    # CLL
    'absolute_lymphocyte_count', 'serum_beta2_microglobulin_level',
    'binet_stage', 'tumor_burden', 'disease_activity',
    'bone_marrow_involvement', 'hepatomegaly', 'splenomegaly', 'lymphadenopathy',
    'btk_inhibitor_refractory', 'bcl2_inhibitor_refractory', 'lymphocyte_doubling_time',
    # Lymphoma
    'flipi_score', 'gelf_criteria_status', 'tumor_grade',
    # Assessment
    'best_response', 'measurable_disease_by_recist_status',
    # Procedures
    'prior_procedures',
]


# LOINC code → (PatientInfo field name, coercion function)
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
    # CMP
    '17861-6': ('serum_calcium_mg_dl',            float),
    '2160-0':  ('serum_creatinine_mg_dl',         float),
    '2164-2':  ('creatinine_clearance_ml_min',    float),
    '62238-1': ('egfr_ml_min_173m2',              float),
    '3094-0':  ('bun_mg_dl',                      float),
    '2951-2':  ('sodium_meq_l',                   float),
    '2823-3':  ('potassium_meq_l',                float),
    '2601-3':  ('magnesium_mg_dl',                float),
    # LFT / cardiac
    '1975-2':  ('bilirubin_total_mg_dl',          float),
    '1742-6':  ('alt_u_l',                        int),
    '1920-8':  ('ast_u_l',                        int),
    '6768-6':  ('alkaline_phosphatase_u_l',       int),
    '1751-7':  ('albumin_g_dl',                   float),
    '2885-2':  ('total_protein',                  float),
    '10839-9': ('troponin_ng_ml',                 float),
    '42637-9': ('bnp_pg_ml',                      int),
    '2345-7':  ('glucose_mg_dl',                  int),
    '4548-4':  ('hba1c_percent',                  float),
    '2532-0':  ('ldh_u_l',                        int),
    # Other markers
    '1952-1':  ('beta2_microglobulin',            float),
    '1988-5':  ('c_reactive_protein',             float),
    '30341-2': ('esr',                            int),
}

# Source-value fallback map for environments where LOINC Concepts aren't loaded.
# Key  = measurement_source_value (as stored by the FHIR upload pipeline or PATCH write-through)
# Value = PatientInfo field name
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
}


def _clear_derived_fields(patient_info: PatientInfo) -> None:
    """Reset all OMOP-derived fields to None so deletions are reflected."""
    for field in _OMOP_DERIVED_FIELDS:
        if hasattr(patient_info, field):
            default = [] if field in ('prior_procedures', 'later_therapies', 'genetic_mutations') else None
            setattr(patient_info, field, default)


def refresh_patient_info(person: Person) -> PatientInfo:
    """Derive and upsert PatientInfo from OMOP tables for a given person.

    This is the single source of truth for PatientInfo derivation. It is called
    by:
      - The populate_patient_info management command
      - OMOP post_save signals (ConditionOccurrence, DrugExposure, Measurement, etc.)
      - The FHIR upload endpoint (after writing OMOP records)
    """
    with transaction.atomic():
        try:
            patient_info = PatientInfo.objects.select_for_update().get(person=person)
        except PatientInfo.DoesNotExist:
            patient_info = PatientInfo(person=person)

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
            _get_social_data,
            _get_behavior_data,
            _get_infection_data,
            _get_assessment_data,
            _get_laboratory_data,
            _get_performance_data,
            _get_genetic_mutations,
            _get_cll_data,
            _get_lymphoma_data,
            _get_prior_procedures,
        ]:
            for field, value in section_fn(person).items():
                setattr(patient_info, field, value)

        _compute_derived_fields(patient_info)
        patient_info.save()
        return patient_info


# ---------------------------------------------------------------------------
# Section extractors — each returns a dict of {field_name: value}
# ---------------------------------------------------------------------------

def _get_demographics(person: Person) -> dict:
    data = {}

    if person.year_of_birth:
        today = date.today()
        data['patient_age'] = today.year - person.year_of_birth

    if person.gender_concept:
        gender_name = person.gender_concept.concept_name.lower()
        if 'male' in gender_name and 'female' not in gender_name:
            data['gender'] = 'M'
        elif 'female' in gender_name:
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


# Clinical-interpretation aliases: raw OMOP condition concept names that EXACT's
# matcher does not recognise, mapped to the canonical disease titles it gates on
# (ADR 0001 — CTOMOP owns the canonical disease title that SoC / ht-phr / EXACT all
# read). Keyed by lowercased concept name; only exact matches are remapped, so
# unrelated conditions pass through untouched.
_DISEASE_ALIASES = {
    'myeloma': 'multiple myeloma',
}


def _canonicalize_disease(name: str) -> str:
    """Map a raw OMOP condition concept name to EXACT's canonical disease title.

    Unrecognised names are returned unchanged (preserve, don't drop).
    """
    if not name:
        return name
    return _DISEASE_ALIASES.get(name.strip().lower(), name)


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

    if most_recent_condition and most_recent_condition.condition_status_concept:
        status_name = most_recent_condition.condition_status_concept.concept_name.lower()
        if 'remission' in status_name:
            data['condition_clinical_status'] = 'remission'
        elif 'relapse' in status_name or 'recur' in status_name:
            data['condition_clinical_status'] = 'relapse'
        elif 'active' in status_name:
            data['condition_clinical_status'] = 'active'
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

    drug_exposures = DrugExposure.objects.filter(person=person).order_by('-drug_exposure_start_date')

    if not drug_exposures.exists():
        return data

    recent_drugs = drug_exposures[:10]

    unique_dates = set(drug.drug_exposure_start_date for drug in drug_exposures)
    data['therapy_lines_count'] = len(unique_dates)

    current_meds = []
    for drug in recent_drugs[:5]:
        if drug.drug_concept:
            current_meds.append(drug.drug_concept.concept_name)
    if current_meds:
        data['concomitant_medications'] = ', '.join(current_meds)

    # Try Episode-based therapy line grouping first
    try:
        from omop_oncology.models import Episode
        episodes = Episode.objects.filter(person=person).order_by('episode_number')
        if episodes.exists():
            return _get_treatment_data_from_episodes(person, data, episodes, drug_exposures)
    except Exception:
        pass

    # Fallback: group by start date so combination regimens count as one line.
    # Sort chronologically: earliest date = line 1.
    from collections import defaultdict
    date_groups = defaultdict(list)
    for drug in drug_exposures:
        name = drug.drug_concept.concept_name if drug.drug_concept else 'Unknown'
        date_groups[drug.drug_exposure_start_date].append(name)

    therapy_details = [
        {
            'drug': ' + '.join(date_groups[d]),
            'start_date': str(d),
            'end_date': None,
        }
        for d in sorted(date_groups.keys())
    ]

    if len(therapy_details) >= 1:
        data['first_line_therapy'] = therapy_details[0]['drug']
        data['first_line_date'] = therapy_details[0]['start_date']

    if len(therapy_details) >= 2:
        data['second_line_therapy'] = therapy_details[1]['drug']
        data['second_line_date'] = therapy_details[1]['start_date']

    if len(therapy_details) > 2:
        later_drugs = therapy_details[2:]
        data['later_therapy'] = '; '.join([d['drug'] for d in later_drugs[:3]])
        data['later_date'] = later_drugs[0]['start_date']
        data['later_therapies'] = [
            {'therapy': d['drug'], 'startDate': d['start_date'], 'endDate': d['end_date']}
            for d in later_drugs
        ]

    return data


def _get_treatment_data_from_episodes(person, data, episodes, drug_exposures):
    """Use Episode records for structured therapy line grouping."""
    try:
        from omop_oncology.models import EpisodeEvent
    except ImportError:
        return data

    for episode in episodes:
        lot = episode.episode_number
        if lot is None:
            continue

        # Get drug names for this episode via EpisodeEvent
        event_drug_ids = EpisodeEvent.objects.filter(
            episode=episode,
        ).values_list('event_id', flat=True)

        drugs_in_episode = DrugExposure.objects.filter(
            drug_exposure_id__in=event_drug_ids,
        ).select_related('drug_concept')

        drug_names = ' + '.join(
            de.drug_concept.concept_name
            for de in drugs_in_episode
            if de.drug_concept
        ) or 'Unknown'

        start_date = str(episode.episode_start_date) if episode.episode_start_date else None
        end_date = str(episode.episode_end_date) if episode.episode_end_date else None

        if lot == 1:
            data['first_line_therapy'] = drug_names
            data['first_line_date'] = start_date
            if end_date:
                data['first_line_end_date'] = end_date
        elif lot == 2:
            data['second_line_therapy'] = drug_names
            data['second_line_date'] = start_date
            if end_date:
                data['second_line_end_date'] = end_date
        elif lot >= 3:
            later = data.get('later_therapies', [])
            later.append({'therapy': drug_names, 'startDate': start_date, 'endDate': end_date})
            data['later_therapies'] = later
            if not data.get('later_therapy'):
                data['later_therapy'] = drug_names
            if not data.get('later_date'):
                data['later_date'] = start_date

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

    for vital_type, loinc_code in vital_sign_concepts.items():
        try:
            concept = Concept.objects.filter(
                concept_code=loinc_code,
                vocabulary__vocabulary_id='LOINC'
            ).first()
            if concept:
                measurement = Measurement.objects.filter(
                    person=person,
                    measurement_concept=concept,
                    value_as_number__isnull=False
                ).order_by('-measurement_date').first()
                if measurement:
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
        except Exception:
            continue

    return data


def _get_biomarker_data(person: Person) -> dict:
    data = {}

    measurements = Measurement.objects.filter(person=person).order_by('-measurement_date')

    pdl1_measurements = measurements.filter(
        measurement_concept__concept_code__in=['85337-4']
    )
    if pdl1_measurements.exists():
        pdl1_test = pdl1_measurements.first()
        data['pd_l1_tumor_cells'] = int(pdl1_test.value_as_number) if pdl1_test.value_as_number else None
        data['pd_l1_assay'] = pdl1_test.value_source_value

    er_measurements = measurements.filter(measurement_concept__concept_code='16112-5')
    if er_measurements.exists():
        er_test = er_measurements.first()
        if er_test.value_as_concept_id:
            concept = Concept.objects.get(pk=er_test.value_as_concept_id)
            if 'positive' in concept.concept_name.lower():
                data['estrogen_receptor_status'] = 'POSITIVE'
            elif 'negative' in concept.concept_name.lower():
                data['estrogen_receptor_status'] = 'NEGATIVE'

    pr_measurements = measurements.filter(measurement_concept__concept_code='16113-3')
    if pr_measurements.exists():
        pr_test = pr_measurements.first()
        if pr_test.value_as_concept_id:
            concept = Concept.objects.get(pk=pr_test.value_as_concept_id)
            if 'positive' in concept.concept_name.lower():
                data['progesterone_receptor_status'] = 'POSITIVE'
            elif 'negative' in concept.concept_name.lower():
                data['progesterone_receptor_status'] = 'NEGATIVE'

    her2_measurements = measurements.filter(measurement_concept__concept_code='48676-1')
    if her2_measurements.exists():
        her2_test = her2_measurements.first()
        if her2_test.value_as_concept_id:
            concept = Concept.objects.get(pk=her2_test.value_as_concept_id)
            if 'positive' in concept.concept_name.lower():
                data['her2_status'] = 'POSITIVE'
            elif 'negative' in concept.concept_name.lower():
                data['her2_status'] = 'NEGATIVE'

    if 'estrogen_receptor_status' in data and 'progesterone_receptor_status' in data and 'her2_status' in data:
        data['tnbc_status'] = (
            data['estrogen_receptor_status'] == 'NEGATIVE'
            and data['progesterone_receptor_status'] == 'NEGATIVE'
            and data['her2_status'] == 'NEGATIVE'
        )

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

    return data


def _get_infection_data(person: Person) -> dict:
    data = {}

    measurements = Measurement.objects.filter(person=person)

    hiv_measurements = measurements.filter(
        measurement_concept__concept_code__in=['5221-7', '7917-8']
    )
    for m in hiv_measurements:
        if m.value_as_concept_id:
            concept = Concept.objects.get(pk=m.value_as_concept_id)
            if 'negative' in concept.concept_name.lower():
                data['no_hiv_status'] = True
                data['hiv_status'] = False
            elif 'positive' in concept.concept_name.lower():
                data['no_hiv_status'] = False
                data['hiv_status'] = True

    hepb_measurements = measurements.filter(
        measurement_concept__concept_code__in=['5195-3']
    )
    for m in hepb_measurements:
        if m.value_as_concept_id:
            concept = Concept.objects.get(pk=m.value_as_concept_id)
            if 'negative' in concept.concept_name.lower():
                data['no_hepatitis_b_status'] = True
                data['hepatitis_b_status'] = False
            elif 'positive' in concept.concept_name.lower():
                data['no_hepatitis_b_status'] = False
                data['hepatitis_b_status'] = True

    hepc_measurements = measurements.filter(
        measurement_concept__concept_code__in=['5196-1']
    )
    for m in hepc_measurements:
        if m.value_as_concept_id:
            concept = Concept.objects.get(pk=m.value_as_concept_id)
            if 'negative' in concept.concept_name.lower():
                data['no_hepatitis_c_status'] = True
                data['hepatitis_c_status'] = False
            elif 'positive' in concept.concept_name.lower():
                data['no_hepatitis_c_status'] = False
                data['hepatitis_c_status'] = True

    return data


def _get_assessment_data(person: Person) -> dict:
    data = {}

    observations = Observation.objects.filter(person=person).order_by('-observation_date')

    response_obs = observations.filter(
        observation_concept__concept_code__in=[
            '182840001', '182841002', '182843004', '182842009',
        ]
    )
    if response_obs.exists():
        response_map = {
            '182840001': 'Complete Response',
            '182841002': 'Partial Response',
            '182843004': 'Stable Disease',
            '182842009': 'Progressive Disease',
        }
        code = response_obs.first().observation_concept.concept_code
        if code in response_map:
            data['best_response'] = response_map[code]

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

    measurements = Measurement.objects.filter(person=person).order_by('-measurement_date')

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
        measurement_concept__vocabulary_id='LOINC',
        measurement_concept__concept_code__in=_LOINC_LAB_FIELDS.keys(),
        value_as_number__isnull=False,
    ).select_related('measurement_concept')
    for m in loinc_ms:
        code = m.measurement_concept.concept_code
        field, cast = _LOINC_LAB_FIELDS[code]
        if field not in data:
            data[field] = cast(m.value_as_number)

    # --- New UI fields via LOINC code stored as source_value (FHIR upload path) ---
    unfound = {f for (f, _) in _LOINC_LAB_FIELDS.values() if f not in data}
    if unfound:
        loinc_sv_ms = measurements.filter(
            measurement_source_value__in=_LOINC_LAB_FIELDS.keys(),
            value_as_number__isnull=False,
        )
        for m in loinc_sv_ms:
            field, cast = _LOINC_LAB_FIELDS[m.measurement_source_value]
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

    observations = Observation.objects.filter(person=person).order_by('-observation_date')

    for obs in observations:
        if not obs.observation_concept:
            continue
        concept_name = obs.observation_concept.concept_name.lower()
        if 'ecog' in concept_name and obs.value_as_number is not None:
            data['ecog_performance_status'] = int(obs.value_as_number)
            break
        elif 'karnofsky' in concept_name and obs.value_as_number is not None:
            data['karnofsky_performance_score'] = int(obs.value_as_number)
            break

    return data


def _get_genetic_mutations(person: Person) -> dict:
    data = {}

    genetic_loinc_codes = {
        '21636-6': 'BRCA1',
        '21637-4': 'BRCA2',
        '21667-1': 'TP53',
        '48013-7': 'KRAS',
        '62862-8': 'EGFR',
        '62318-1': 'PIK3CA',
    }

    origin_concepts = {255395001: 'germline', 255461003: 'somatic'}
    interpretation_concepts = {30166007: 'pathogenic', 10828004: 'benign', 42425007: 'vus'}

    mutations = []

    genetic_measurements = Measurement.objects.filter(
        person=person,
        measurement_concept__concept_code__in=genetic_loinc_codes.keys()
    ).order_by('-measurement_date')

    for measurement in genetic_measurements:
        if not measurement.value_as_string:
            continue
        gene = genetic_loinc_codes.get(measurement.measurement_concept.concept_code)
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
    measurements = Measurement.objects.filter(person=person).order_by('-measurement_date')
    observations = Observation.objects.filter(person=person).order_by('-observation_date')
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
    observations = Observation.objects.filter(person=person).order_by('-observation_date')
    measurements = Measurement.objects.filter(person=person).order_by('-measurement_date')

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
        if 'grade' in cname and 'lymphoma' in cname and m.value_as_number is not None:
            data['tumor_grade'] = int(m.value_as_number)

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

def _compute_derived_fields(patient_info: PatientInfo) -> None:
    """Compute fields that depend on other PatientInfo fields being set."""
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
