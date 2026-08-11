# omop_core/services/mappings.py
from omop_core.models import Concept

# Maps PatientRecord field name → (LOINC code, unit string, display name)
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
    'creatinine_mg_dl':               ('2160-0',   'mg/dL',           'Creatinine [Mass/volume] in Serum or Plasma'),
    'serum_calcium_mg_dl':            ('17861-6',  'mg/dL',           'Calcium [Mass/volume] in Serum or Plasma'),
    'calcium_mg_dl':                  ('17861-6',  'mg/dL',           'Calcium [Mass/volume] in Serum or Plasma'),
    'egfr_ml_min_173m2':              ('62238-1',  'mL/min/1.73m2',   'GFR/BSA pred CKD-EPI ArA'),
    'egfr':                           ('62238-1',  'mL/min/1.73m2',   'GFR/BSA pred CKD-EPI ArA'),
    'bun_mg_dl':                      ('3094-0',   'mg/dL',           'Urea nitrogen [Mass/volume] in Serum or Plasma'),
    'blood_urea_nitrogen':            ('3094-0',   'mg/dL',           'Urea nitrogen [Mass/volume] in Serum or Plasma'),
    'sodium_meq_l':                   ('2951-2',   'mEq/L',           'Sodium [Moles/volume] in Serum or Plasma'),
    'serum_sodium':                   ('2951-2',   'mEq/L',           'Sodium [Moles/volume] in Serum or Plasma'),
    'potassium_meq_l':                ('2823-3',   'mEq/L',           'Potassium [Moles/volume] in Serum or Plasma'),
    'serum_potassium':                ('2823-3',   'mEq/L',           'Potassium [Moles/volume] in Serum or Plasma'),
    'magnesium_mg_dl':                ('2601-3',   'mg/dL',           'Magnesium [Mass/volume] in Serum or Plasma'),
    'magnesium':                      ('2601-3',   'mg/dL',           'Magnesium [Mass/volume] in Serum or Plasma'),
    'phosphorus':                     ('2777-1',   'mg/dL',           'Phosphate [Mass/volume] in Serum or Plasma'),
    # Liver function
    'bilirubin_total_mg_dl':          ('1975-2',   'mg/dL',           'Bilirubin.total [Mass/volume] in Serum or Plasma'),
    'alt_u_l':                        ('1742-6',   'U/L',             'Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma'),
    'ast_u_l':                        ('1920-8',   'U/L',             'Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline_phosphatase_u_l':       ('6768-6',   'U/L',             'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma'),
    'alkaline_phosphatase':           ('6768-6',   'U/L',             'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma'),
    'albumin_g_dl':                   ('1751-7',   'g/dL',            'Albumin [Mass/volume] in Serum or Plasma'),
    'total_protein':                  ('2885-2',   'g/dL',            'Protein [Mass/volume] in Serum or Plasma'),
    'troponin_ng_ml':                 ('10839-9',  'ng/mL',           'Troponin I.cardiac [Mass/volume] in Serum or Plasma'),
    'bnp_pg_ml':                      ('42637-9',  'pg/mL',           'BNP [Mass/volume] in Serum or Plasma'),
    'glucose_mg_dl':                  ('2345-7',   'mg/dL',           'Glucose [Mass/volume] in Serum or Plasma'),
    'hba1c_percent':                  ('4548-4',   '%',               'Hemoglobin A1c/Hemoglobin.total in Blood'),
    'inr':                            ('6301-6',   '{INR}',           'INR in Platelet poor plasma'),
    'pt_seconds':                     ('5902-2',   's',               'Prothrombin time (PT)'),
    'ptt_seconds':                    ('3173-2',   's',               'aPTT in Platelet poor plasma'),
    # Oncology markers
    'ldh_u_l':                        ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
    'ldh_level':                      ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
    'ldh':                            ('2532-0',   'U/L',             'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma'),
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

CONDITION_FIELDS = frozenset({'disease', 'stage', 'condition_code_icd_10', 'condition_code_snomed_ct'})

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
CONCEPT_GENERIC_LAB       = 3000963   # Laboratory test result (fallback)
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

# Activity trend thresholds: % change between first-half and second-half means
WEARABLE_TREND_IMPROVING_PCT = 10.0
WEARABLE_TREND_DECLINING_PCT = -10.0


def get_gender_concept(gender_str):
    """Map a gender string to an OMOP Concept. Returns None if not found."""
    if not gender_str:
        return None
    gender_map = {
        'male': 8507, 'm': 8507,
        'female': 8532, 'f': 8532,
        'unknown': 8551, 'other': 8551, 'ambiguous': 8570,
    }
    concept_id = gender_map.get(gender_str.lower().strip())
    if concept_id:
        try:
            return Concept.objects.get(concept_id=concept_id)
        except Concept.DoesNotExist:
            return None
    return None
