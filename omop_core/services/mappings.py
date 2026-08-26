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
CONCEPT_PATIENT_REPORTED_TYPE = 32865  # Patient self-report (measurement/observation type, vocab 'Type Concept')


# Slice 2a: staging PatientInfo fields written as patient-authored *string* Measurements, keyed by
# the LOINC the derivation reads back. The staging reader returns value_as_string verbatim, so a CB
# stage code (e.g. 't1') round-trips unchanged and the matcher compares it to the trial's code list
# directly — no normalisation or matching-surface reverse-map needed.
# (Receptor biomarkers her2/er/pr are a SEPARATE follow-up (2a-ii): they must be normalised to
# Positive/Negative/Equivocal for the hr_status/tnbc_status compute, which then needs a lossy
# PromopMatchingSurface reverse-map back to the CB enum codes the matcher compares. Not in this change.)
STAGING_MEAS_FIELDS = {
    'stage': '21908-9',
    'tumor_stage': '21905-5',
    'nodes_stage': '21906-3',
    'distant_metastasis_stage': '21901-4',
}


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
#   genuinely          btk_inhibitor_refractory and bcl2_inhibitor_refractory
#   ambiguous          both read SNOMED 182842009; the code alone cannot say
#                      which drug failed
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
    # Assertion booleans — _get_assertion_data. Made writable via their EXISTING LOINC assertion codes
    # (the same codes _ASSERTION_FIELDS already reads), so a federated edit round-trips through the
    # existing reader with no second path to clobber it, and the patient's dated edit supersedes an
    # older FHIR assertion of the same concept. Only the two DIRECT booleans here; the inverse_boolean
    # no_* fields need write-side inversion, deferred to healthkey-ai/promop#699.
    'contraceptive_use':             ('8659-8',    'LOINC',  '_get_assertion_data'),
    'consent_capability':            ('75985-6',   'LOINC',  '_get_assertion_data'),
}
