# omop_core/services/mappings.py
from omop_core.models import Concept

# Maps PatientRecord field name → (LOINC code, unit string, display name)
#
# Each LOINC code MUST appear at most once (issue #471). Nine codes were
# previously mapped by two or three field names each, causing write collisions
# (same Measurement row overwritten), stale projection values, and consumer
# confusion. The duplicate entries were removed; only the canonical
# unit-suffixed field name is retained. Legacy field names are still populated
# during derivation — see _LAB_FIELD_ALIASES in patient_record_service.py.
#
# Deduplicated fields (canonical ← removed aliases):
#   serum_calcium_mg_dl    ← calcium_mg_dl
#   serum_creatinine_mg_dl ← creatinine_mg_dl
#   egfr_ml_min_173m2      ← egfr
#   bun_mg_dl              ← blood_urea_nitrogen
#   sodium_meq_l           ← serum_sodium
#   potassium_meq_l        ← serum_potassium
#   magnesium_mg_dl        ← magnesium
#   alkaline_phosphatase_u_l ← alkaline_phosphatase
#   ldh_u_l                ← ldh_level, ldh
LAB_FIELD_TO_LOINC = {
    # Blood counts
    'hemoglobin_g_dl':                ('718-7',    'g/dL',            'Hemoglobin [Mass/volume] in Blood'),
    'hematocrit_percent':             ('20570-8',  '%',               'Hematocrit [Volume Fraction] of Blood'),
    'wbc_count_thousand_per_ul':      ('6690-2',   '10*3/uL',         'Leukocytes [#/volume] in Blood'),
    'rbc_million_per_ul':             ('789-8',    '10*6/uL',         'Erythrocytes [#/volume] in Blood'),
    'platelet_count_thousand_per_ul': ('777-3',    '10*3/uL',         'Platelets [#/volume] in Blood'),
    'anc_thousand_per_ul':            ('751-8',    '10*3/uL',         'Neutrophils [#/volume] in Blood'),
    'alc_thousand_per_ul':            ('731-0',    '10*3/uL',         'Lymphocytes [#/volume] in Blood'),
    'amc_thousand_per_ul':            ('742-7',    '10*3/uL',         'Monocytes [#/volume] in Blood'),
    # Kidney / electrolytes
    'serum_creatinine_mg_dl':         ('2160-0',   'mg/dL',           'Creatinine [Mass/volume] in Serum or Plasma'),
    'serum_calcium_mg_dl':            ('17861-6',  'mg/dL',           'Calcium [Mass/volume] in Serum or Plasma'),
    'egfr_ml_min_173m2':              ('62238-1',  'mL/min/1.73m2',   'GFR/BSA pred CKD-EPI ArA'),
    'bun_mg_dl':                      ('3094-0',   'mg/dL',           'Urea nitrogen [Mass/volume] in Serum or Plasma'),
    'sodium_meq_l':                   ('2951-2',   'mEq/L',           'Sodium [Moles/volume] in Serum or Plasma'),
    'potassium_meq_l':                ('2823-3',   'mEq/L',           'Potassium [Moles/volume] in Serum or Plasma'),
    'magnesium_mg_dl':                ('2601-3',   'mg/dL',           'Magnesium [Mass/volume] in Serum or Plasma'),
    'phosphorus':                     ('2777-1',   'mg/dL',           'Phosphate [Mass/volume] in Serum or Plasma'),
    # Liver function
    'bilirubin_total_mg_dl':          ('1975-2',   'mg/dL',           'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'serum_bilirubin_level_direct':  ('1968-7',   'mg/dL',           'Bilirubin.direct [Mass/volume] in Serum or Plasma'),
    'alt_u_l':                        ('1742-6',   'U/L',             'Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma'),
    'ast_u_l':                        ('1920-8',   'U/L',             'Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline_phosphatase_u_l':       ('6768-6',   'U/L',             'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma'),
    'albumin_g_dl':                   ('1751-7',   'g/dL',            'Albumin [Mass/volume] in Serum or Plasma'),
    'total_protein':                  ('2885-2',   'g/dL',            'Protein [Mass/volume] in Serum or Plasma'),
    'troponin_ng_ml':                 ('10839-9',  'ng/mL',           'Troponin I.cardiac [Mass/volume] in Serum or Plasma'),
    'bnp_pg_ml':                      ('42637-9',  'pg/mL',           'BNP [Mass/volume] in Serum or Plasma'),
    'glucose_mg_dl':                  ('2345-7',   'mg/dL',           'Glucose [Mass/volume] in Serum or Plasma'),
    'hba1c_percent':                  ('4548-4',   '%',               'Hemoglobin A1c/Hemoglobin.total in Blood'),
    'inr':                            ('6301-6',   '{INR}',           'INR in Platelet poor plasma'),
    'pt_seconds':                     ('5902-2',   's',               'Prothrombin time (PT)'),
    'ptt_seconds':                    ('3173-2',   's',               'aPTT in Platelet poor plasma'),
    'cea_ng_ml':                      ('2039-6',   'ng/mL',           'Carcinoembryonic Ag [Mass/volume] in Serum or Plasma'),
    'ca19_9_u_ml':                    ('25390-6',  'U/mL',            'Cancer Ag 19-9 [Units/volume] in Serum or Plasma'),
    'psa_ng_ml':                      ('2857-1',   'ng/mL',           'Prostate specific Ag [Mass/volume] in Serum or Plasma'),
    # Oncology markers
    'ldh_u_l':                        ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
    'beta2_microglobulin':            ('1952-1',   'mg/L',            'Beta-2-Microglobulin [Mass/volume] in Serum or Plasma'),
    'c_reactive_protein':             ('1988-5',   'mg/L',            'C reactive protein [Mass/volume] in Serum or Plasma'),
    'esr':                            ('30341-2',  'mm/h',            'Erythrocyte sedimentation rate'),
    'ki67_proliferation_index':       ('85319-2',  '%',               'Ki-67 Ag [Presence] in Tissue by Immune stain'),
    # Vital signs
    'weight':                         ('29463-7',  'kg',              'Body weight'),
    'height':                         ('8302-2',   'cm',              'Body height'),
    'systolic_blood_pressure':        ('8480-6',   'mm[Hg]',          'Systolic blood pressure'),
    'diastolic_blood_pressure':       ('8462-4',   'mm[Hg]',          'Diastolic blood pressure'),
    'heartrate':                      ('8867-4',   '/min',            'Heart rate'),
    # Performance status
    'ecog_performance_status':        ('89247-1',  '{score}',         'ECOG Performance Status score'),
    'karnofsky_performance_score':    ('89243-0',  '{score}',         'Karnofsky Performance Status score'),
}

# Reverse lookup: legacy alias → canonical field name. Used by the write-
# through so a PATCH to a legacy field name still creates the correct
# Measurement row (issue #471).
LAB_FIELD_ALIAS_TO_CANONICAL = {
    'calcium_mg_dl':        'serum_calcium_mg_dl',
    'creatinine_mg_dl':     'serum_creatinine_mg_dl',
    'egfr':                 'egfr_ml_min_173m2',
    'blood_urea_nitrogen':  'bun_mg_dl',
    'serum_sodium':         'sodium_meq_l',
    'serum_potassium':      'potassium_meq_l',
    'magnesium':            'magnesium_mg_dl',
    'alkaline_phosphatase': 'alkaline_phosphatase_u_l',
    'ldh_level':            'ldh_u_l',
    'ldh':                  'ldh_u_l',
}

# Common unit options for fields where multiple units are used in US clinical
# practice. The first entry is the US default. Used by the mapping UI to
# render a unit dropdown.
FIELD_COMMON_UNITS: dict[str, list[str]] = {
    # Blood counts
    'hemoglobin_g_dl':                ['g/dL', 'g/L', 'mmol/L'],
    'hematocrit_percent':             ['%'],
    'wbc_count_thousand_per_ul':      ['10*3/uL', '10*9/L'],
    'rbc_million_per_ul':             ['10*6/uL', '10*12/L'],
    'platelet_count_thousand_per_ul': ['10*3/uL', '10*9/L'],
    'anc_thousand_per_ul':            ['10*3/uL', '10*9/L'],
    'alc_thousand_per_ul':            ['10*3/uL', '10*9/L'],
    'amc_thousand_per_ul':            ['10*3/uL', '10*9/L'],
    # Kidney / electrolytes
    'serum_creatinine_mg_dl':         ['mg/dL', 'umol/L'],
    'serum_calcium_mg_dl':            ['mg/dL', 'mmol/L'],
    'egfr_ml_min_173m2':              ['mL/min/1.73m2'],
    'bun_mg_dl':                      ['mg/dL', 'mmol/L'],
    'sodium_meq_l':                   ['mEq/L', 'mmol/L'],
    'potassium_meq_l':                ['mEq/L', 'mmol/L'],
    'magnesium_mg_dl':                ['mg/dL', 'mmol/L', 'mEq/L'],
    'phosphorus':                     ['mg/dL', 'mmol/L'],
    # Liver function
    'bilirubin_total_mg_dl':          ['mg/dL', 'umol/L'],
    'serum_bilirubin_level_direct':   ['mg/dL', 'umol/L'],
    'alt_u_l':                        ['U/L'],
    'ast_u_l':                        ['U/L'],
    'alkaline_phosphatase_u_l':       ['U/L'],
    'albumin_g_dl':                   ['g/dL', 'g/L'],
    'total_protein':                  ['g/dL', 'g/L'],
    'troponin_ng_ml':                 ['ng/mL', 'ng/L', 'pg/mL'],
    'bnp_pg_ml':                      ['pg/mL', 'ng/L'],
    'glucose_mg_dl':                  ['mg/dL', 'mmol/L'],
    'hba1c_percent':                  ['%', 'mmol/mol'],
    'inr':                            ['{INR}'],
    'pt_seconds':                     ['s'],
    'ptt_seconds':                    ['s'],
    'cea_ng_ml':                      ['ng/mL', 'ug/L'],
    'ca19_9_u_ml':                    ['U/mL', 'kU/L'],
    'psa_ng_ml':                      ['ng/mL', 'ug/L'],
    # Oncology markers
    'ldh_u_l':                        ['U/L'],
    'beta2_microglobulin':            ['mg/L', 'nmol/L'],
    'c_reactive_protein':             ['mg/L', 'mg/dL'],
    'esr':                            ['mm/h'],
    'ki67_proliferation_index':       ['%'],
    # Myeloma markers
    'monoclonal_protein_serum':       ['g/dL', 'g/L'],
    'monoclonal_protein_urine':      ['mg/24h', 'mg/day'],
    'kappa_flc':                      ['mg/L', 'mg/dL'],
    'lambda_flc':                     ['mg/L', 'mg/dL'],
    'kappa_lambda_ratio':             ['{ratio}'],
    'clonal_plasma_cells':            ['%'],
    # CLL / lymphoma disease markers
    'absolute_lymphocyte_count':      ['10*3/uL', '10*9/L'],
    'clonal_b_lymphocyte_count':      ['10*3/uL', '10*9/L'],
    'clonal_bone_marrow_b_lymphocytes': ['%'],
    'largest_lymph_node_size':        ['cm', 'mm'],
    'lymphocyte_doubling_time':       ['mo'],
    'spleen_size':                    ['cm'],
    'qtcf_value':                     ['ms'],
    # Breast cancer / tumor markers
    'tumor_size':                     ['cm', 'mm'],
    'pd_l1_tumor_cells':              ['%'],
    'pd_l1_ic_percentage':            ['%'],
    'pd_l1_combined_positive_score':  ['{score}'],
    # Lab aliases (legacy fields that mirror canonical ones)
    'albumin_level':                  ['g/dL', 'g/L'],
    'hemoglobin_level':               ['g/dL', 'g/L', 'mmol/L'],
    'serum_bilirubin_level_total':    ['mg/dL', 'umol/L'],
    'serum_calcium_level':            ['mg/dL', 'mmol/L'],
    'serum_creatinine_level':         ['mg/dL', 'umol/L'],
    'serum_beta2_microglobulin_level': ['mg/L', 'nmol/L'],
    'white_blood_cell_count':         ['10*3/uL', '10*9/L'],
    'absolute_neutrophile_count':     ['10*3/uL', '10*9/L'],
    'platelet_count':                 ['10*3/uL', '10*9/L'],
    'red_blood_cell_count':           ['10*6/uL', '10*12/L'],
    'creatinine_clearance_ml_min':    ['mL/min'],
    # Cardiac
    'ejection_fraction':              ['%'],
    'heartrate_variability':          ['ms'],
    # Behavior
    'sleep_hours_per_night':          ['h'],
    # Vital signs
    'weight':                         ['kg', 'lbs'],
    'height':                         ['cm', 'in'],
    'systolic_blood_pressure':        ['mm[Hg]'],
    'diastolic_blood_pressure':       ['mm[Hg]'],
    'heartrate':                      ['/min'],
    # Performance status
    'ecog_performance_status':        ['{score}'],
    'karnofsky_performance_score':    ['{score}'],
}

# Reusable UCUM-oriented choices for a curator when a field has no narrower
# clinical unit set.  FIELD_COMMON_UNITS always takes precedence and is ordered
# with the usual US reporting unit first.
STANDARD_UNIT_CHOICES = [
    '%', 'kg', 'cm', 'mm[Hg]', 'mg/dL', 'g/dL', 'g/L', 'mg/L', 'ng/mL',
    'pg/mL', 'U/L', 'U/mL', 'mEq/L', 'mmol/L', 'umol/L', '10*3/uL',
    '10*6/uL', 'mL/min/1.73m2', 'mm/h', 's', '/min', '{score}', '{INR}',
]

CONDITION_FIELDS = frozenset({'disease', 'stage', 'condition_code_icd_10', 'condition_code_snomed_ct'})

# Fields that trigger _sync_demographics. Note that it only writes gender and
# the birth date to Person — `patient_age` is a function of the birth date and
# `ethnicity` is unhandled — so membership here does NOT mean a field survives
# a re-derivation.
DEMOGRAPHIC_FIELDS = frozenset({'gender', 'date_of_birth', 'patient_age', 'ethnicity'})

# Maps line number (1/2/3) → PatientRecord field prefix
THERAPY_LINE_PREFIXES = {
    1: 'first_line',
    2: 'second_line',
    3: 'later',
}

THERAPY_LINE_FIELDS = frozenset(
    f'{prefix}_{suffix}'
    for prefix in THERAPY_LINE_PREFIXES.values()
    for suffix in ('therapy', 'start_date', 'end_date', 'outcome', 'intent', 'discontinuation_reason')
)

# OMOP concept IDs used by the sync service
# Fallback for a lab with no resolvable concept. OMOP CDM reserves concept_id 0
# ("No matching concept") for exactly this, and it is the only id guaranteed not
# to mean something else.
#
# This was 3000963, seeded locally as a placeholder named "Generic Lab
# Measurement". The seed reasoned that vocabulary_id='None' and concept_code='0'
# kept it from being matched by LOINC lookups — true, but beside the point. The
# collision is on the *id*: Athena owns 3000963 as "Hemoglobin [Mass/volume] in
# Blood" (LOINC 718-7). Loading a real vocabulary silently turned every unmapped
# lab ever written into a haemoglobin result, and derivation duly projected them
# — 3,773 such rows on staging, 116,219 on a dev box. See
# remap_generic_lab_fallback for the repair.
CONCEPT_GENERIC_LAB       = 0         # No matching concept (OMOP CDM sentinel)
CONCEPT_LAB_TYPE          = 32856     # Lab (measurement type)
CONCEPT_EHR_TYPE          = 32817     # EHR (condition type)
CONCEPT_TREATMENT_REGIMEN = 32531     # Treatment Regimen (episode concept)
CONCEPT_DRUG_EXPOSURE_FIELD = 1147094  # drug_exposure_id field concept (EpisodeEvent)


# Wearable metric → controlled-vocabulary concept_code.
#
# Most entries are LOINC. Four metrics have no LOINC equivalent and are minted
# locally under the HK-Wearable vocabulary (see WEARABLE_CONCEPT_VOCAB), so this
# map is NOT LOINC-only despite most of its contents.
#
# Every code here is verified to resolve to a concept whose meaning matches the
# metric. Do not add an entry without checking the concept_name in Athena — four
# codes in the original version resolved to BMI and body-fat-percentage concepts.
WEARABLE_CONCEPT_CODE = {
    'steps':              '55423-8',   # Number of steps in unspecified time Pedometer
    'active_minutes':     '55411-3',   # Exercise duration
    'resting_hr':         '40443-4',   # Heart rate --resting
    # HRV is two distinct metrics, not one. SDNN (standard deviation of the
    # R-R series) and RMSSD (root mean square of successive differences) are
    # different statistics over the same signal and are NOT interchangeable.
    # 80404-7 is specifically the standard-deviation form, so only a source
    # that genuinely reports SDNN may be mapped to it — Apple's
    # HKQuantityTypeIdentifierHeartRateVariabilitySDNN does; Garmin's HRV
    # Status does not (it is RMSSD). See #438.
    'hrv_sdnn':           '80404-7',   # R-R interval.standard deviation (HRV SDNN)
    'hrv_rmssd':          'HK-WEAR-HRV-RMSSD',  # no LOINC equivalent — see below
    'spo2':               '59408-5',   # Oxygen saturation in Arterial blood by Pulse oximetry
    'respiratory_rate':   '9279-1',    # Respiratory rate
    'sleep_duration':     '93832-4',   # Sleep duration
    'vo2_max':            '94122-9',   # Oxygen consumption (VO2)/Body weight
    'distance':           '41953-1',   # Walking distance 24 hour Calculated
    'walking_speed':      '41957-2',   # Walking speed 24 hour mean Calculated
    'walking_step_length': 'HK-WEAR-STEP-LENGTH',        # no LOINC equivalent
    'walking_double_support_pct': 'HK-WEAR-DBL-SUPPORT',  # no LOINC equivalent
    'walking_hr_avg':     'HK-WEAR-WALK-HR',             # no LOINC equivalent
    'flights_climbed':    '100304-5',  # Flights climbed [#] Reporting Period
    'active_energy':      '93819-1',   # Calories burned in unspecified time --during activity
    'basal_energy':       'HK-WEAR-BASAL-ENERGY',        # no LOINC equivalent
    'body_mass':          '29463-7',   # Body weight
}

# Vocabulary each code above belongs to. Concept resolution must be scoped by
# (vocabulary_id, concept_code) — a bare concept_code is ambiguous, since 852
# codes are reused across vocabularies.
WEARABLE_CONCEPT_VOCAB = {
    metric: ('HK-Wearable' if code.startswith('HK-') else 'LOINC')
    for metric, code in WEARABLE_CONCEPT_CODE.items()
}

# Metrics whose concept is Observation-domain and therefore belong in the
# `observation` table rather than `measurement`. This mirrors the domain_id of
# the concepts above and exists for tests and fixtures; the runtime write path
# reads concept.domain_id directly so it stays correct if a code changes.
WEARABLE_OBSERVATION_METRICS = frozenset({
    'steps', 'active_minutes', 'sleep_duration', 'flights_climbed',
})

# Artifact-filter bounds: readings outside [lo, hi] are discarded before aggregation
WEARABLE_ARTIFACT_BOUNDS = {
    'spo2':             (70.0, 100.0),
    'resting_hr':       (20.0, 300.0),
    'hrv_sdnn':         (1.0,  300.0),
    'hrv_rmssd':        (1.0,  300.0),
    'respiratory_rate': (4.0,  60.0),
    'steps':            (0.0,  100_000.0),
    'active_minutes':   (0.0,  1440.0),
    'sleep_duration':   (0.0,  24.0),
    'vo2_max':          (10.0, 100.0),
    'distance':         (0.0,  100.0),     # km/day
    'walking_speed':    (0.5,  15.0),      # km/hr
    'walking_step_length': (20.0, 200.0),  # cm
    'walking_double_support_pct': (5.0, 80.0),  # %
    'walking_hr_avg':   (30.0, 220.0),     # bpm
    'flights_climbed':  (0.0,  200.0),     # flights/day
    'active_energy':    (0.0,  10000.0),   # kcal/day
    'basal_energy':     (500.0, 5000.0),   # kcal/day
    'body_mass':        (20.0, 300.0),     # kg
}

# Provenance type concept for wearable rows: 32865 'Patient self-report'.
#
# OMOP's Type Concept vocabulary contains no device or wearable type — all 81
# rows were reviewed against a full Athena load — so this is the closest
# faithful fit for data produced by the patient's own device. It is seeded by
# seed_omop_concepts with its genuine Athena concept_id.
#
# Do not reintroduce a fallback here. The previous code used 32883 ('Survey')
# and fell back to 32856 ('Lab'), mislabelling every wearable row's provenance
# (#441).
WEARABLE_TYPE_CONCEPT_ID = 32865

# Minimum valid days required to emit a metric (else field stays None)
WEARABLE_MIN_VALID_DAYS = 7


def resolve_wearable_mappings(device_type):
    """Build metric_key → Concept from approved SourceCodeConceptMapping rows.

    For Apple uploads, the SCCM source_code is the HK identifier (e.g.,
    HKQuantityTypeIdentifierStepCount); we reverse-map through _APPLE_TYPE_MAP
    to get the internal metric_key.

    For Garmin uploads, the SCCM source_code IS the metric_key (e.g., 'steps').

    There is intentionally no code-to-concept fallback. An absent SCCM row is
    a mapping/configuration gap, not permission for the importer to revive a
    second, invisible mapping registry in Python.

    Returns:
        dict mapping metric_key → Concept (or None if unresolvable)
    """
    from omop_core.models import SourceCodeConceptMapping

    source_vocab = 'Apple' if device_type == 'apple' else 'Garmin'

    # Query all approved mappings for this device vocabulary.
    approved = SourceCodeConceptMapping.objects.filter(
        source_vocabulary_id=source_vocab,
        status='approved',
    ).select_related('target_concept')

    db_mappings = {}
    if device_type == 'apple':
        # Build reverse map: HK identifier → metric_key
        from omop_core.services.wearable_parsers import _APPLE_TYPE_MAP
        hk_to_metric = {hk_id: mkey for hk_id, mkey in _APPLE_TYPE_MAP.items()}
        # Also handle sleep (category type, not in _APPLE_TYPE_MAP quantity map)
        hk_to_metric['HKCategoryTypeIdentifierSleepAnalysis'] = 'sleep_duration'

        for row in approved:
            metric_key = hk_to_metric.get(row.source_code)
            if metric_key and row.target_concept:
                db_mappings[metric_key] = row.target_concept
    else:
        # Garmin: source_code == metric_key
        for row in approved:
            if row.target_concept:
                db_mappings[row.source_code] = row.target_concept

    return db_mappings

# Activity trend thresholds: % change between first-half and second-half means
WEARABLE_TREND_IMPROVING_PCT = 10.0
WEARABLE_TREND_DECLINING_PCT = -10.0


def get_gender_concept(gender_str):
    """Map a gender string to an OMOP Concept. Returns None if not found.

    Delegates to the demographics resolver, which looks concepts up by
    (vocabulary_id, concept_code) — the natural key. This used to hold its own
    table of concept_ids (8507, 8532, 8551, 8570). Those ids are correct today and
    were correct when written, which is exactly what makes the pattern dangerous:
    an id belongs to a vocabulary release, and the same assumption applied to 3000963
    turned every unmapped lab into a haemoglobin result once Athena was loaded.

    The mapping itself is unchanged, deliberately — including 'other' resolving to
    UNKNOWN rather than to the OTHER concept that also exists. Repointing it would
    change the meaning of every FHIR import and is a separate decision.
    """
    from omop_core.services.demographics import resolve_concept

    return resolve_concept('gender', gender_str)


# PatientRecord field → the concept its derivation reads, recovered from the
# extractors rather than chosen by hand.
#
# Provenance. If derivation reads code X into field F, then writing X is correct
# by construction: a round trip through derivation returns the same value. That
# makes these mappings auditable — the third element names the extractor the
# attribution came from, so a reviewer can check the claim at its source instead
# of re-deriving it. They were found by AST-walking patient_record_service.py for
# `data['field'] = ...` assignments and the code literal governing them.
#
# The one rule that governs membership: a code here must be claimed by exactly
# one field, across this table AND LAB_FIELD_TO_LOINC. Ten further fields were
# attributed to a single code and deliberately left out because another field
# claims the same code — writing either would overwrite the other's row, which is
# the collision #471 removed. They fall into three shapes, all needing review:
#
#   legacy duplicate   white_blood_cell_count shares 6690-2 with
#                      wbc_count_thousand_per_ul; biopsy_grade_depr shares
#                      44648-4 with biopsy_grade
#   two parts of one   pd_l1_assay and pd_l1_tumor_cells are the assay and the
#   fact               numeric result of ONE 83052-1 measurement, as are
#                      test_methodology and oncotype_dx_score of one 85337-4
#                      report, and ecog_assessment_date is the date of the
#                      ecog_performance_status observation
#   resolved (#785)    btk_inhibitor_refractory and bcl2_inhibitor_refractory
#                      both read SNOMED 182842009, which cannot say which drug
#                      class failed. Migrations 0180/0181 mint a source concept
#                      per class and map it; the SNOMED entries below remain the
#                      read path for records written before that.
#
# field → (concept_code, vocabulary_id, attributed_from_extractor)
DERIVED_FIELD_TO_CODE = {
    # Biomarkers — _get_biomarker_data
    'bone_only_metastasis_status':   ('44667-4',   'LOINC',  '_get_biomarker_data'),
    'estrogen_receptor_status':      ('16112-5',   'LOINC',  '_get_biomarker_data'),
    'her2_status':                   ('48676-1',   'LOINC',  '_get_biomarker_data'),
    'histologic_type':               ('59847-4',   'LOINC',  '_get_biomarker_data'),
    'progesterone_receptor_status':  ('16113-3',   'LOINC',  '_get_biomarker_data'),
    'pd_l1_combined_positive_score': ('83054-7',   'LOINC',  '_get_biomarker_data'),
    'pd_l1_ic_percentage':           ('83055-4',   'LOINC',  '_get_biomarker_data'),
    # Genomics / pathology — _get_genomics_pathology_data
    # Derivation historically read 82185-1, which is not a LOINC code — it
    # resolves against no vocabulary release, so a fact written under it could
    # never carry a concept. 49457-5 ("Androgen receptor Ag [Presence] in Tissue
    # by Immune stain") is the only standard LOINC concept for the analyte, and
    # [Presence] matches this column holding a qualitative Positive/Negative.
    # Derivation now reads 49457-5 first and 82185-1 second, so the round trip
    # holds and any pre-existing row still projects.
    'androgen_receptor_status':      ('49457-5',   'LOINC',  '_get_genomics_pathology_data'),
    'lymph_node_status':             ('92837-4',   'LOINC',  '_get_genomics_pathology_data'),
    'metastasis_status':             ('21907-1',   'LOINC',  '_get_genomics_pathology_data'),
    'report_interpretation':         ('69548-6',   'LOINC',  '_get_genomics_pathology_data'),
    'test_specimen_type':            ('31208-2',   'LOINC',  '_get_genomics_pathology_data'),
    # Staging — _get_staging_data
    'distant_metastasis_stage':      ('21901-4',   'LOINC',  '_get_staging_data'),
    'nodes_stage':                   ('21906-3',   'LOINC',  '_get_staging_data'),
    'stage':                         ('21908-9',   'LOINC',  '_get_staging_data'),
    'tumor_stage':                   ('21905-5',   'LOINC',  '_get_staging_data'),
    # CLL — _get_cll_data. 21889-1 is 'Size Tumor'; a lymph-node row carries
    # qualifier_source_value='lymph-node' to separate it from tumor_size.
    'largest_lymph_node_size':       ('21889-1',   'LOINC',  '_get_cll_data'),
    # Social — _get_social_data
    # #596 corrected _get_social_data: 408729009 had been writing to
    # concomitant_medication_details, which is what this attribution was
    # recovered from. The attribution was right about the code and wrong
    # about the field the moment the bug was fixed — see
    # test_every_attribution_still_matches_its_extractor.
    'insurance_type':                ('408729009', 'SNOMED', '_get_social_data'),
}

# Curator-oriented concept suggestions for fields NOT in DERIVED_FIELD_TO_CODE
# or LAB_FIELD_TO_LOINC. These generate yellow "proposed" flags in the mapping
# UI but are NOT used by the derivation or write-through pipelines. Codes may
# be shared across fields (no uniqueness constraint).
#
# field → (concept_code, vocabulary_id)
SUGGESTED_FIELD_CODES: dict[str, tuple[str, str]] = {
    # Myeloma labs / markers
    'monoclonal_protein_serum':      ('51435-3',   'LOINC'),
    'monoclonal_protein_urine':      ('51436-1',   'LOINC'),
    'kappa_flc':                     ('11050-2',   'LOINC'),
    'lambda_flc':                    ('11051-0',   'LOINC'),
    'kappa_lambda_ratio':            ('11052-8',   'LOINC'),
    'clonal_plasma_cells':           ('24133-4',   'LOINC'),
    'myeloma_type':                  ('64197005',  'SNOMED'),
    'r_iss_stage':                   ('21908-9',   'LOINC'),
    'mrd_status':                    ('98847-0',   'LOINC'),
    'progression':                   ('246450006', 'SNOMED'),
    # Myeloma CRAB criteria
    'hypercalcemia':                 ('66931009',  'SNOMED'),
    'renal_impairment':              ('723188008', 'SNOMED'),
    'anemia':                        ('271737000', 'SNOMED'),
    'bone_lesions':                  ('363817008', 'SNOMED'),
    # SCT
    'stem_cell_transplant_history':  ('77465005',  'SNOMED'),
    'sct_eligibility':               ('183851006', 'SNOMED'),
    # Breast cancer
    'tnbc_status':                   ('706886006', 'SNOMED'),
    'hr_status':                     ('416053008', 'SNOMED'),
    'oncotype_dx_score':             ('85337-4',   'LOINC'),
    'menopausal_status':             ('276498001', 'SNOMED'),
    # Lymphoma
    'flipi_score':                   ('444723004', 'SNOMED'),
    'gelf_criteria_status':          ('109964006', 'SNOMED'),
    'bulky_disease':                 ('277578007', 'SNOMED'),
    'b_symptoms':                    ('89268003',  'SNOMED'),
    'bone_marrow_involvement':       ('24940005',  'SNOMED'),
    'number_of_nodal_sites':         ('370130003', 'SNOMED'),
    'tumor_grade':                   ('371469007', 'SNOMED'),
    # CLL
    'binet_stage':                   ('106241006', 'SNOMED'),
    'tumor_burden':                  ('246923001', 'SNOMED'),
    'disease_activity':              ('246456005', 'SNOMED'),
    'richter_transformation':        ('91860004',  'SNOMED'),
    'splenomegaly':                  ('16294009',  'SNOMED'),
    'hepatomegaly':                  ('80515008',  'SNOMED'),
    'lymphadenopathy':               ('30746006',  'SNOMED'),
    'tp53_disruption':               ('405835008', 'SNOMED'),
    # Social / behavioral
    'smoking_status':                ('72166-2',   'LOINC'),
    'pack_years':                    ('401201003', 'SNOMED'),
    'alcohol_use':                   ('74013-4',   'LOINC'),
    'exercise_frequency':            ('77592-7',   'LOINC'),
    'employment_status':             ('364703007', 'SNOMED'),
    'education_level':               ('105421008', 'SNOMED'),
    'marital_status':                ('125680007', 'SNOMED'),
    # Negation fields (map to the base clinical concept being negated)
    'no_active_infection_status':    ('56051006',  'SNOMED'),
    'no_hiv_status':                 ('86406008',  'SNOMED'),
    'no_hepatitis_b_status':         ('66071002',  'SNOMED'),
    'no_hepatitis_c_status':         ('50711007',  'SNOMED'),
    # Conditions
    'hiv_status':                    ('86406008',  'SNOMED'),
    'hepatitis_b_status':            ('66071002',  'SNOMED'),
    'hepatitis_c_status':            ('50711007',  'SNOMED'),
    # Aligned with the mapping migration 0182 seeds (#723). _build_suggestion
    # runs unconditionally, so a different code here would show a suggestion
    # permanently disagreeing with the field's own recorded mapping.
    'preexisting_conditions':        ('102478008', 'SNOMED'),
    # Treatment
    'refractory_status':             ('182854000', 'SNOMED'),
    'relapse_count':                 ('263855007', 'SNOMED'),
    # Disease metadata
    'disease':                       ('29308-4',   'LOINC'),
    'diagnosis_date':                ('52832-8',   'LOINC'),
    'condition_clinical_status':     ('33999-4',   'LOINC'),
    # Staging
    'staging_modalities':            ('399390009', 'SNOMED'),
    'measurable_disease_by_recist_status': ('711259004', 'SNOMED'),
    # Cytogenetics
    'cytogenetic_risk':              ('405825005', 'SNOMED'),
    'cytogenetic_abnormalities':     ('409709004', 'SNOMED'),
    # Other
    'peripheral_neuropathy_grade':   ('302226006', 'SNOMED'),
    'toxicity_grade':                ('246112005', 'SNOMED'),

    # ── Additional fields to cover all PatientRecord mappable fields ──

    # Labs / measurements (duplicates or alternate names for existing fields)
    'albumin_level':                 ('1751-7',    'LOINC'),   # Albumin [Mass/volume] in Serum or Plasma
    'hemoglobin_level':              ('718-7',     'LOINC'),   # Hemoglobin [Mass/volume] in Blood
    'platelet_count':                ('777-3',     'LOINC'),   # Platelets [#/volume] in Blood
    'liver_enzyme_levels':           ('1742-6',    'LOINC'),   # ALT [Enzymatic activity/volume] in Serum
    'serum_bilirubin_level_total':   ('1975-2',    'LOINC'),   # Bilirubin.total [Mass/volume] in Serum
    'serum_calcium_level':           ('17861-6',   'LOINC'),   # Calcium [Mass/volume] in Serum
    'serum_creatinine_level':        ('2160-0',    'LOINC'),   # Creatinine [Mass/volume] in Serum
    'serum_beta2_microglobulin_level': ('32731-2', 'LOINC'),   # Beta-2-Microglobulin [Mass/volume]
    'bone_imaging_result':           ('24646-7',   'LOINC'),   # Bone XR study
    'pulmonary_function_test_result': ('19858-0',  'LOINC'),   # Spirometry panel
    'metastatic_status':             ('399584008', 'SNOMED'),  # Metastasis status
    'renal_adequacy_status':         ('723188008', 'SNOMED'),  # Renal impairment

    # Pregnancy / reproductive
    'pregnancy_test_result':         ('2106-3',    'LOINC'),   # Choriogonadotropin [Presence] in Urine
    'pregnancy_test_date':           ('2106-3',    'LOINC'),   # same LOINC

    # Disease / staging
    'stage':                         ('21908-9',   'LOINC'),   # Stage group
    'tumor_stage':                   ('21905-5',   'LOINC'),   # Tumor stage
    'lymph_node_status':             ('21906-3',   'LOINC'),   # Regional lymph nodes
    'nodes_stage':                   ('21906-3',   'LOINC'),   # N stage
    'distant_metastasis_stage':      ('21907-1',   'LOINC'),   # Distant metastasis
    'metastasis_status':             ('21907-1',   'LOINC'),   # M stage
    'histologic_type':               ('59847-4',   'LOINC'),   # Histology type Cancer
    'biopsy_grade':                  ('33732-9',   'LOINC'),   # Histology grade
    'biopsy_grade_depr':             ('33732-9',   'LOINC'),   # deprecated biopsy_grade

    # Breast cancer specifics
    'estrogen_receptor_status':      ('16112-5',   'LOINC'),   # Estrogen receptor [Interpretation]
    'progesterone_receptor_status':  ('16113-3',   'LOINC'),   # Progesterone receptor [Interpretation]
    'her2_status':                   ('48676-1',   'LOINC'),   # HER2 [Interpretation]
    'androgen_receptor_status':      ('85310-1',   'LOINC'),   # AR receptor
    'ki67_proliferation_index':      ('29593-1',   'LOINC'),   # Ki-67
    'hrd_status':                    ('94077-5',   'LOINC'),   # Homologous recombination deficiency
    'pd_l1_assay':                   ('85147-7',   'LOINC'),   # PD-L1 by immunohistochemistry
    'tumor_size':                    ('21889-1',   'LOINC'),   # Tumor size
    'bone_only_metastasis_status':   ('21907-1',   'LOINC'),   # Distant metastasis
    'pd_l1_tumor_cells':             ('85147-7',   'LOINC'),   # PD-L1 Cells
    'pd_l1_ic_percentage':           ('85146-9',   'LOINC'),   # PD-L1 Immune cells
    'pd_l1_combined_positive_score': ('96267-2',   'LOINC'),   # PD-L1 Combined Positive Score

    # CLL / lymphoma
    'clonal_b_lymphocyte_count':     ('30374-0',   'LOINC'),   # B cell count
    'clonal_bone_marrow_b_lymphocytes': ('30374-0', 'LOINC'),  # B cell count
    'lymphocyte_doubling_time':      ('26474-7',   'LOINC'),   # Lymphocytes [#/volume]
    'autoimmune_cytopenias_refractory_to_steroids': ('439478003', 'SNOMED'),  # Autoimmune cytopenia
    'btk_inhibitor_refractory':      ('182854000', 'SNOMED'),  # Treatment refractory
    'bcl2_inhibitor_refractory':     ('182854000', 'SNOMED'),  # Treatment refractory
    'measurable_disease_iwcll':      ('711259004', 'SNOMED'),  # Measurable disease
    'measurable_disease_imwg':       ('711259004', 'SNOMED'),  # Measurable disease
    'post_transformation_outcome':   ('91860004',  'SNOMED'),  # Richter / transformation
    'dlbcl_transformation_date':     ('91860004',  'SNOMED'),  # Richter / transformation
    'transformed_to_dlbcl':          ('91860004',  'SNOMED'),  # Richter / transformation
    'plasma_cell_leukemia':          ('47082-2',   'LOINC'),   # Plasma cells in bone marrow
    'largest_lymph_node_size':       ('21889-1',   'LOINC'),   # Size of primary tumor
    'spleen_size':                   ('16294009',  'SNOMED'),  # Splenomegaly
    'flipi_score_options':           ('444723004', 'SNOMED'),  # FLIPI

    # Genomics / molecular
    'genetic_mutations':             ('55232-3',   'LOINC'),   # Genetic analysis summary panel
    'molecular_markers':             ('55232-3',   'LOINC'),   # Genetic analysis summary panel
    'cytogenic_markers':             ('D002869',   'MeSH'),    # Chromosome Aberrations (#803)
    'protein_expressions':           ('85337-4',   'LOINC'),   # Gene expression panel

    # Demographics / profile
    'date_of_birth':                 ('21112-8',   'LOINC'),   # Birth date
    'gender':                        ('46098-0',   'LOINC'),   # Sex
    'race':                          ('32624-9',   'LOINC'),   # Race
    'ethnicity':                     ('69490-1',   'LOINC'),   # Ethnicity OMB
    'patient_age':                   ('30525-0',   'LOINC'),   # Age
    'phone_number':                  ('42077-8',   'LOINC'),   # Phone number
    'email':                         ('76435-7',   'LOINC'),   # Telecom email
    'facility_name':                 ('69476-0',   'LOINC'),   # Facility name
    # Aligned with migration 0182 (#774); see the note on preexisting_conditions.
    'languages_skills':              ('61909002',  'SNOMED'),  # Language

    # Behavioral
    'consent_capability':            ('405193005', 'SNOMED'),  # Ability to consent
    'contraceptive_use':             ('13197004',  'SNOMED'),  # Contraception
    'geographic_exposure_risk_details': ('420008001', 'SNOMED'),  # Travel
    'substance_use_details':         ('66214007',  'SNOMED'),  # Substance abuse
    'tobacco_use_details':           ('365981007', 'SNOMED'),  # Tobacco use finding

    # Treatment / therapy line fields -- REMOVED in #707.
    # These fields are computed (derived from Episode + DrugExposure records),
    # not concept-mapped. Entries were: line_of_therapy, prior_therapy,
    # last_treatment, planned_therapies, supportive_therapies,
    # washout_period_duration, remission_duration_min, prior_procedures,
    # treatment_refractory_status, reason_for_discontinuation, therapy_intent,
    # first_line_*, second_line_*, later_*, supportive_therapy_*,
    # concomitant_medication_*, therapy_component_ids, therapy_ids_provenance,
    # therapy_type_ids.

    # Condition codes
    'condition_code_icd_10':         ('29308-4',   'LOINC'),   # Diagnosis
    'condition_code_snomed_ct':      ('29308-4',   'LOINC'),   # Diagnosis

    # Test metadata
    'test_date':                     ('33882-2',   'LOINC'),   # Specimen collection date
    'test_methodology':              ('85069-3',   'LOINC'),   # Test methodology
    'test_specimen_type':            ('66746-9',   'LOINC'),   # Specimen type
    'report_interpretation':         ('69115-4',   'LOINC'),   # Report interpretation

    # Assessment dates
    'ecog_assessment_date':          ('89247-1',   'LOINC'),   # ECOG
    'sct_date':                      ('77465005',  'SNOMED'),  # SCT

    # Insurance / admin
    'insurance_type':                ('76437-3',   'LOINC'),   # Insurance type
    'annual_household_income':       ('77244-2',   'LOINC'),   # Annual income

    # Misc admin/profile
    'suppress_demographics_for_others': ('445313000', 'SNOMED'),  # Privacy/consent
    'death_date':                    ('93036-3',   'LOINC'),   # Date of death
    'number_of_dependents':          ('63514-1',   'LOINC'),   # Number of dependents

    # Other vitals / cardiac
    'heartrate_variability':         ('80404-7',   'LOINC'),   # HRV SDNN
    'ejection_fraction':             ('10230-1',   'LOINC'),   # Ejection fraction
    'qtcf_value':                    ('8632-1',    'LOINC'),   # QTcF interval

    # Additional social
    'sleep_hours_per_night':         ('93832-4',   'LOINC'),   # Sleep duration
    'sleep_quality':                 ('93831-6',   'LOINC'),   # Sleep quality
    'exercise_minutes_per_week':     ('77592-7',   'LOINC'),   # Exercise
    'drinks_per_week':               ('74013-4',   'LOINC'),   # Alcohol use
    'diet_type':                     ('81659-4',   'LOINC'),   # Diet
    'stress_level':                  ('93025-6',   'LOINC'),   # Stress level
    'social_support':                ('93029-8',   'LOINC'),   # Social support
    'caregiver_availability_status': ('93030-6',   'LOINC'),   # Caregiver
}
