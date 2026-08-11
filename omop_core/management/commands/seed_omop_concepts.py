"""
Seed the minimal set of OMOP CDM concepts required for the mCODE FHIR import
pipeline to write Measurements, ConditionOccurrences, and DrugExposures and for
refresh_patient_record to derive PatientRecord fields from them.

This is a dev/test shortcut.  Production environments should load concepts via
the full load_athena_vocabularies command instead.

Usage:
    DATABASE_URL=... python manage.py seed_omop_concepts
"""
from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import Concept, ConceptClass, Domain, Vocabulary


# ---------------------------------------------------------------------------
# Minimal vocabularies, domains, and concept-classes required by the rows below
# ---------------------------------------------------------------------------

_VOCABULARIES = [
    dict(vocabulary_id='Type Concept',
         vocabulary_name='OMOP Type Concept',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
    dict(vocabulary_id='LOINC',
         vocabulary_name='Logical Observation Identifiers Names and Codes',
         vocabulary_reference='https://loinc.org',
         vocabulary_version='2.76',
         vocabulary_concept_id=0),
    dict(vocabulary_id='SNOMED',
         vocabulary_name='SNOMED Clinical Terms',
         vocabulary_reference='https://www.snomed.org',
         vocabulary_version='2024-09-01',
         vocabulary_concept_id=0),
    dict(vocabulary_id='CDM',
         vocabulary_name='OMOP CDM',
         vocabulary_reference='OMOP generated',
         vocabulary_version='CDM v5',
         vocabulary_concept_id=0),
    # Locally-authored wearable concepts with no LOINC equivalent. Rows in this
    # vocabulary always carry source='HealthKey' and concept_id >= 2e9.
    dict(vocabulary_id='HK-Wearable',
         vocabulary_name='HealthKey Wearable Metrics',
         vocabulary_reference='HealthKey local mint',
         vocabulary_version='v1',
         vocabulary_concept_id=0),
    dict(vocabulary_id='Episode',
         vocabulary_name='OMOP Episode',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
    dict(vocabulary_id='Gender',
         vocabulary_name='OMOP Gender',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
    # OMOP's placeholder vocabulary. Required by concept 0 ('No matching
    # concept') and by the generic-lab fallback 3000963. It was referenced by
    # _CONCEPTS but never seeded, so seeding against an empty database raised
    # IntegrityError — see conftest.py, which documents working around this.
    dict(vocabulary_id='None',
         vocabulary_name='OMOP Standardized Vocabularies',
         vocabulary_reference='OMOP generated',
         vocabulary_version='v5',
         vocabulary_concept_id=0),
    dict(vocabulary_id='HemOnc',
         vocabulary_name='HemOnc',
         vocabulary_reference='https://hemonc.org',
         vocabulary_version='2024',
         vocabulary_concept_id=0),
]

_DOMAINS = [
    dict(domain_id='Type Concept', domain_name='Type Concept', domain_concept_id=0),
    dict(domain_id='Measurement',  domain_name='Measurement',   domain_concept_id=0),
    dict(domain_id='Condition',    domain_name='Condition',      domain_concept_id=0),
    dict(domain_id='Meas Value',   domain_name='Meas Value',     domain_concept_id=0),
    dict(domain_id='Episode',      domain_name='Episode',        domain_concept_id=0),
    dict(domain_id='Observation',  domain_name='Observation',    domain_concept_id=0),
    dict(domain_id='Metadata',     domain_name='Metadata',       domain_concept_id=0),
    dict(domain_id='Gender',       domain_name='Gender',          domain_concept_id=0),
]

_CONCEPT_CLASSES = [
    dict(concept_class_id='Type Concept',       concept_class_name='Type Concept',         concept_class_concept_id=0),
    dict(concept_class_id='Lab Test',            concept_class_name='Lab Test',             concept_class_concept_id=0),
    dict(concept_class_id='Clinical Observation', concept_class_name='Clinical Observation', concept_class_concept_id=0),
    dict(concept_class_id='Disorder',            concept_class_name='Disorder',             concept_class_concept_id=0),
    dict(concept_class_id='Qualifier Value',     concept_class_name='Qualifier Value',      concept_class_concept_id=0),
    dict(concept_class_id='Field',               concept_class_name='Field',                concept_class_concept_id=0),
    dict(concept_class_id='Treatment',           concept_class_name='Treatment',            concept_class_concept_id=0),
    dict(concept_class_id='Gender',              concept_class_name='Gender',               concept_class_concept_id=0),
    dict(concept_class_id='Regimen',             concept_class_name='Regimen',              concept_class_concept_id=0),
    dict(concept_class_id='Undefined',           concept_class_name='Undefined',            concept_class_concept_id=0),
]


# ---------------------------------------------------------------------------
# Core concepts
# ---------------------------------------------------------------------------

_START = date(1970, 1, 1)
_END   = date(2099, 12, 31)


def _c(concept_id, concept_name, domain_id, vocabulary_id, concept_class_id,
        standard_concept, concept_code,
        valid_start=None, valid_end=None, source=None):
    """Build a concept row.

    ``source`` follows Concept.source semantics: None means the row mirrors an
    externally-loaded vocabulary release (Athena), 'HealthKey' means it is
    authored locally. Locally-authored rows MUST use an HK-* vocabulary and a
    concept_id in the OHDSI custom range (>= 2e9) — see _assert_local_mint.
    """
    return dict(
        concept_id=concept_id,
        concept_name=concept_name,
        domain_id=domain_id,
        vocabulary_id=vocabulary_id,
        concept_class_id=concept_class_id,
        standard_concept=standard_concept,
        concept_code=concept_code,
        valid_start_date=valid_start or _START,
        valid_end_date=valid_end or _END,
        invalid_reason=None,
        source=source,
    )


# OHDSI reserves concept_id >= 2e9 for locally-authored concepts; Athena never
# allocates there. HK-Labs occupies 2029606279-2029606349 on staging, so the
# wearable block continues immediately above it.
LOCAL_CONCEPT_ID_MIN = 2_000_000_000
_HK_WEARABLE_ID_BASE = 2_029_606_350


def _hk(offset, concept_name, domain_id, concept_class_id, concept_code):
    """Build a locally-minted HK-Wearable concept, correctly quarantined."""
    return _c(
        _HK_WEARABLE_ID_BASE + offset, concept_name, domain_id, 'HK-Wearable',
        concept_class_id, None, concept_code, source='HealthKey',
    )


_CONCEPTS = [
    # ------------------------------------------------------------------
    # concept 0 — OMOP's universal "No matching concept" sentinel, written to
    # any *_concept_id when source data cannot be mapped. It is domain-agnostic
    # by design: on staging it is referenced by 12,352 observations, 8,131 drug
    # exposures, 290 conditions and 14 measurements.
    #
    # It must NOT carry a real vocabulary or domain. Storing it as
    # (HK-Labs, Measurement, Lab Test) — as one database had — makes every
    # unmapped row of every domain look like a HealthKey lab test to any query
    # that groups by vocabulary or domain. See #427.
    # ------------------------------------------------------------------
    _c(0, 'No matching concept', 'Metadata', 'None', 'Undefined', None, 'No matching concept'),

    # ------------------------------------------------------------------
    # Gender concepts — needed for Person.gender_concept FK.
    # ------------------------------------------------------------------
    _c(8507, 'MALE',    'Gender', 'Gender', 'Gender', 'S', 'M'),
    _c(8532, 'FEMALE',  'Gender', 'Gender', 'Gender', 'S', 'F'),
    _c(8551, 'UNKNOWN', 'Gender', 'Gender', 'Gender', 'S', 'U'),

    # ------------------------------------------------------------------
    # Type concepts — needed for measurement_type_concept_id and
    # condition_type_concept_id FK fields in every OMOP event table.
    # ------------------------------------------------------------------
    _c(32817, 'EHR',               'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976890'),
    _c(32856, 'Lab',               'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976929'),
    # Provenance for patient-generated data (wearable uploads). OMOP's Type
    # Concept vocabulary has no device/wearable type — all 81 rows were
    # reviewed — so 'Patient self-report' is the closest faithful fit for data
    # the patient's own device produced. Previously wearable rows were typed
    # 32883, which is 'Survey' (#441).
    _c(32865, 'Patient self-report', 'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976938'),
    _c(32869, 'Pharmacy claim',    'Type Concept', 'Type Concept', 'Type Concept', 'S', 'OMOP4976942'),
    _c(32531, 'Treatment Regimen', 'Episode',       'Episode',       'Treatment',    'S', 'OMOP4822256'),

    # CDM metadata concept used in DrugExposure FK lookups
    _c(1147094, 'drug_exposure.drug_exposure_id', 'Metadata', 'CDM', 'Field', 'S', 'CDM150'),

    # ------------------------------------------------------------------
    # Generic lab fallback — used when no specific LOINC concept is found.
    # concept_id 3000963 is pre-hoisted in upload_fhir as _concept_generic_lab.
    # vocabulary_id='None' and concept_code='0' are intentional non-LOINC
    # placeholders so this concept is never matched by LOINC code lookups
    # and cannot pollute specific lab fields (e.g. hemoglobin_g_dl).
    # ------------------------------------------------------------------
    _c(3000963, 'Generic Lab Measurement', 'Measurement', 'None', 'Lab Test', 'S', '0'),

    # ------------------------------------------------------------------
    # Breast cancer condition
    # ------------------------------------------------------------------
    _c(4112853, 'Malignant tumor of breast', 'Condition', 'SNOMED', 'Disorder', 'S', '254837009'),

    # ------------------------------------------------------------------
    # Lymphoma conditions — FL and DLBCL (HemOnc concept_ids match Athena;
    # verified against staging DB). DLBCL ConditionOccurrence is the primary
    # evidence for FL → DLBCL histologic transformation.
    # ------------------------------------------------------------------
    _c(42542169, 'Follicular lymphoma',          'Condition', 'HemOnc', 'Disorder', None, '599'),
    _c(42542162, 'Diffuse large B-cell lymphoma', 'Condition', 'HemOnc', 'Disorder', None, '589'),

    # ------------------------------------------------------------------
    # Tumor marker biomarkers — critical for refresh_patient_record to set
    # estrogen_receptor_status, progesterone_receptor_status, her2_status.
    # ------------------------------------------------------------------
    _c(3004390, 'Estrogen receptor [Interpretation] in Tissue',    'Measurement', 'LOINC', 'Lab Test', 'S', '16112-5'),
    _c(3003289, 'Progesterone receptor [Interpretation] in Tissue', 'Measurement', 'LOINC', 'Lab Test', 'S', '16113-3'),
    _c(3048223, 'HER2 [Interpretation] in Tissue',                 'Measurement', 'LOINC', 'Lab Test', 'S', '48676-1'),

    # Categorical Positive / Negative values stored in value_as_concept_id
    _c(9191, 'Positive', 'Meas Value', 'SNOMED', 'Qualifier Value', 'S', '10828004'),
    _c(9189, 'Negative', 'Meas Value', 'SNOMED', 'Qualifier Value', 'S', '260385009'),

    # ------------------------------------------------------------------
    # TNM staging and cancer disease status
    # ------------------------------------------------------------------
    _c(3022698,  'Stage group.clinical Cancer',    'Measurement',  'LOINC', 'Clinical Observation', 'S', '21908-9'),
    _c(36305408, 'Response to cancer treatment',   'Observation',  'LOINC', 'Clinical Observation', 'S', '88040-1'),

    # ------------------------------------------------------------------
    # CBC — enables CBC fields in PatientRecord via _LOINC_LAB_FIELDS
    # ------------------------------------------------------------------
    _c(3023314, 'Hematocrit [Volume Fraction] of Blood by Automated count', 'Measurement', 'LOINC', 'Lab Test', 'S', '4544-3'),
    _c(3000905, 'Leukocytes [#/volume] in Blood by Automated count',        'Measurement', 'LOINC', 'Lab Test', 'S', '6690-2'),
    _c(3020416, 'Erythrocytes [#/volume] in Blood by Automated count',      'Measurement', 'LOINC', 'Lab Test', 'S', '789-8'),
    _c(3024929, 'Platelets [#/volume] in Blood by Automated count',         'Measurement', 'LOINC', 'Lab Test', 'S', '777-3'),
    _c(3013650, 'Neutrophils [#/volume] in Blood by Automated count',       'Measurement', 'LOINC', 'Lab Test', 'S', '751-8'),
    _c(3004327, 'Lymphocytes [#/volume] in Blood by Automated count',       'Measurement', 'LOINC', 'Lab Test', 'S', '731-0'),
    _c(3033575, 'Monocytes [#/volume] in Blood by Automated count',         'Measurement', 'LOINC', 'Lab Test', 'S', '742-7'),

    # ------------------------------------------------------------------
    # CMP basics
    # ------------------------------------------------------------------
    _c(3016723, 'Creatinine [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '2160-0'),
    _c(3051825, 'Creatinine [Mass/volume] in Blood',           'Measurement', 'LOINC', 'Lab Test', None, '38483-4'),  # mCODE variant
    _c(3006906, 'Calcium [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '17861-6'),
    _c(3032503, 'Calcium [Mass/volume] in Blood',              'Measurement', 'LOINC', 'Lab Test', None, '49765-1'),
    _c(3016436, 'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '2532-0'),
    _c(3000285, 'Sodium [Moles/volume] in Blood',              'Measurement', 'LOINC', 'Lab Test', None, '2947-0'),
    _c(3005456, 'Potassium [Moles/volume] in Blood',           'Measurement', 'LOINC', 'Lab Test', None, '6298-4'),
    _c(3004295, 'Urea nitrogen [Mass/volume] in Blood',        'Measurement', 'LOINC', 'Lab Test', None, '6299-2'),
    _c(3030354, 'Glomerular filtration rate in Serum or Plasma by CKD-EPI',
                                                               'Measurement', 'LOINC', 'Lab Test', None, '33914-3'),
    _c(3000483, 'Glucose [Mass/volume] in Blood',              'Measurement', 'LOINC', 'Lab Test', None, '2339-0'),
    # ------------------------------------------------------------------
    # LFT — present in mCODE synthetic data
    # ------------------------------------------------------------------
    _c(3006923, 'Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '1742-6'),
    _c(3013721, 'Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '1920-8'),
    _c(3024561, 'Albumin [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '1751-7'),
    _c(3024128, 'Bilirubin.total [Mass/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '1975-2'),
    _c(3020630, 'Protein [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '2885-2'),
    _c(3035995, 'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma',
                                                               'Measurement', 'LOINC', 'Lab Test', 'S', '6768-6'),
    _c(3004410, 'Hemoglobin A1c/Hemoglobin.total in Blood',    'Measurement', 'LOINC', 'Lab Test', 'S', '4548-4'),

    # ------------------------------------------------------------------
    # Vitals — enables weight, height, BP, HR in PatientRecord
    # ------------------------------------------------------------------
    _c(3025315, 'Body weight',             'Measurement', 'LOINC', 'Clinical Observation', 'S', '29463-7'),
    _c(3036277, 'Body height',             'Measurement', 'LOINC', 'Clinical Observation', 'S', '8302-2'),
    _c(3004249, 'Systolic blood pressure', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '8480-6'),
    _c(3012888, 'Diastolic blood pressure','Measurement', 'LOINC', 'Clinical Observation', 'S', '8462-4'),
    _c(3027018, 'Heart rate',              'Measurement', 'LOINC', 'Clinical Observation', 'S', '8867-4'),
    _c(3038553, 'Body mass index (BMI) [Ratio]', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '39156-5'),
    _c(3020891, 'Body temperature',        'Measurement', 'LOINC', 'Clinical Observation', 'S', '8310-5'),
    _c(40762499, 'Oxygen saturation in Arterial blood by Pulse oximetry',
                                           'Measurement', 'LOINC', 'Clinical Observation', 'S', '59408-5'),
    _c(40758413, 'Blood pressure systolic and diastolic', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '55284-4'),

    # ------------------------------------------------------------------
    # Clinical assessments
    # ------------------------------------------------------------------
    _c(36305384, 'ECOG Performance Status score', 'Measurement',  'LOINC', 'Clinical Observation', 'S', '89247-1'),
    _c(43054909, 'Tobacco smoking status',         'Observation',  'LOINC', 'Clinical Observation', 'S', '72166-2'),

    # ------------------------------------------------------------------
    # CBC — hemoglobin (missing from original seeder; required for CRAB)
    # ------------------------------------------------------------------
    _c(9001001, 'Hemoglobin [Mass/volume] in Blood',      'Measurement', 'LOINC', 'Lab Test', 'S', '718-7'),

    # ------------------------------------------------------------------
    # MM-specific disease burden labs
    # Required for monoclonal_protein_serum, kappa_flc, lambda_flc,
    # clonal_plasma_cells to be populated by refresh_patient_record.
    # ------------------------------------------------------------------
    _c(9001002, 'Protein [electrophoresis] in Serum',             'Measurement', 'LOINC', 'Lab Test', 'S', '51435-6'),  # M-spike serum
    _c(9001003, 'Protein [electrophoresis] in Urine',             'Measurement', 'LOINC', 'Lab Test', 'S', '32730-5'),  # M-spike urine
    _c(9001004, 'Kappa free light chains [Mass/volume] in Serum', 'Measurement', 'LOINC', 'Lab Test', 'S', '33944-8'),
    _c(9001005, 'Lambda free light chains [Mass/volume] in Serum','Measurement', 'LOINC', 'Lab Test', 'S', '33945-5'),
    _c(9001006, 'Plasma cells [#/volume] in Bone marrow',         'Measurement', 'LOINC', 'Lab Test', 'S', '26098-4'),

    # ------------------------------------------------------------------
    # Beta-2 microglobulin and other MM markers
    # ------------------------------------------------------------------
    _c(9001007, 'Beta-2 microglobulin [Mass/volume] in Serum',    'Measurement', 'LOINC', 'Lab Test', 'S', '1952-1'),
    _c(9001008, 'C reactive protein [Mass/volume] in Serum',      'Measurement', 'LOINC', 'Lab Test', 'S', '1988-5'),
    _c(9001009, 'Erythrocyte sedimentation rate',                 'Measurement', 'LOINC', 'Lab Test', 'S', '30341-2'),
    _c(9001010, 'Creatinine renal clearance',                     'Measurement', 'LOINC', 'Lab Test', 'S', '2164-2'),
    _c(9001011, 'Troponin T [Mass/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '10839-9'),
    _c(9001012, 'Natriuretic peptide B [Mass/volume] in Serum',   'Measurement', 'LOINC', 'Lab Test', 'S', '42637-9'),
    _c(9001013, 'Glucose [Mass/volume] in Serum or Plasma',       'Measurement', 'LOINC', 'Lab Test', 'S', '2345-7'),
    _c(9001014, 'Magnesium [Mass/volume] in Blood',               'Measurement', 'LOINC', 'Lab Test', 'S', '2601-3'),
    _c(9001015, 'Sodium [Moles/volume] in Serum or Plasma',       'Measurement', 'LOINC', 'Lab Test', 'S', '2951-2'),
    _c(9001016, 'Potassium [Moles/volume] in Serum or Plasma',    'Measurement', 'LOINC', 'Lab Test', 'S', '2823-3'),
    _c(9001017, 'Urea nitrogen [Mass/volume] in Serum or Plasma', 'Measurement', 'LOINC', 'Lab Test', 'S', '3094-0'),
    _c(9001018, 'Glomerular filtration rate by CKD-EPI',          'Measurement', 'LOINC', 'Lab Test', 'S', '62238-1'),

    # ------------------------------------------------------------------
    # Wearable / remote-monitoring concepts.
    #
    # These MIRROR genuine Athena rows: concept_id, name, domain and class are
    # copied verbatim from the LOINC vocabulary. Because seeding is keyed on
    # concept_id, a row seeded here is created on a database with no Athena load
    # and matches the existing row where Athena IS loaded — so no duplicate
    # (vocabulary_id, concept_code) pair can arise. Never mint a fresh id for a
    # code Athena already owns; that is what produced the duplicates in #415.
    #
    # Note the domains: steps, active_minutes, sleep_duration and flights_climbed
    # are Observation-domain concepts. upload_wearable routes on concept.domain_id.
    # ------------------------------------------------------------------
    _c(40758552, 'Number of steps in unspecified time Pedometer',              'Observation', 'LOINC', 'Clinical Observation', 'S', '55423-8',  date(2009, 4, 23)),
    _c(40758540, 'Exercise duration',                                          'Observation', 'LOINC', 'Clinical Observation', 'S', '55411-3',  date(2009, 4, 23)),
    _c(3040891,  'Heart rate --resting',                                       'Measurement', 'LOINC', 'Clinical Observation', 'S', '40443-4'),
    _c(21491502, 'R-R interval.standard deviation (Heart rate variability)',   'Measurement', 'LOINC', 'Clinical Observation', 'S', '80404-7',  date(2016, 6, 24)),
    # spo2 (59408-5 / 40762499) and body_mass (29463-7 / 3025315) are already
    # seeded in the vitals block above and must not be repeated here — a second
    # row for the same (vocabulary_id, concept_code) is exactly the duplication
    # this block exists to avoid.
    _c(3024171,  'Respiratory rate',                                           'Measurement', 'LOINC', 'Clinical Observation', 'S', '9279-1',   date(1996, 9, 6)),
    _c(1002368,  'Sleep duration',                                             'Observation', 'LOINC', 'Clinical Observation', 'S', '93832-4',  date(2019, 12, 13)),
    _c(1002246,  'Oxygen consumption (VO2)/Body weight [Volume Rate Content]', 'Measurement', 'LOINC', 'Clinical Observation', 'S', '94122-9',  date(2019, 12, 13)),
    _c(3031111,  'Walking distance 24 hour Calculated',                        'Measurement', 'LOINC', 'Clinical Observation', 'S', '41953-1',  date(2005, 7, 27)),
    _c(3032289,  'Walking speed 24 hour mean Calculated',                      'Measurement', 'LOINC', 'Clinical Observation', 'S', '41957-2',  date(2005, 7, 27)),
    _c(1761351,  'Flights climbed [#] Reporting Period',                       'Observation', 'LOINC', 'Clinical Observation', 'S', '100304-5', date(2022, 8, 8)),
    _c(1001786,  'Calories burned in unspecified time --during activity',      'Measurement', 'LOINC', 'Clinical Observation', 'S', '93819-1',  date(2019, 12, 13)),

    # Wearable metrics with NO LOINC equivalent — locally minted, quarantined
    # under HK-Wearable with source='HealthKey' and ids in the OHDSI custom
    # range. LOINC has no concept for step length, double-support percentage,
    # walking heart rate, or basal energy expenditure in kcal/day (50042-1 is
    # "Basal metabolic rate index", a normalized index, not an energy rate).
    _hk(0, 'Walking step length',                    'Measurement', 'Clinical Observation', 'HK-WEAR-STEP-LENGTH'),
    _hk(1, 'Walking double support time percentage', 'Measurement', 'Clinical Observation', 'HK-WEAR-DBL-SUPPORT'),
    _hk(2, 'Heart rate during walking',              'Measurement', 'Clinical Observation', 'HK-WEAR-WALK-HR'),
    _hk(3, 'Basal energy expenditure',               'Measurement', 'Clinical Observation', 'HK-WEAR-BASAL-ENERGY'),
    # RMSSD has no LOINC concept. Verified against a full Athena load
    # (1,979,416 concepts): searching LOINC for 'RMSSD' and for
    # 'successive difference' returns nothing, and the complete 'R-R interval'
    # family carries only mean, min, max, standard deviation (80404-7 /
    # 76643-6) and coefficient of variation. SDNN is present; RMSSD is not.
    #
    # It must not be aliased onto 80404-7: that code is specifically the
    # standard-deviation form, and filing RMSSD under it is the same defect
    # class as the pre-#413 walking_speed → BMI mapping. See #438.
    _hk(4, 'R-R interval RMSSD (heart rate variability)', 'Measurement', 'Clinical Observation', 'HK-WEAR-HRV-RMSSD'),

    # ------------------------------------------------------------------
    # HemOnc therapy regimen concepts — required for first_line_therapy_id
    # and second_line_therapy_id to be populated by refresh_patient_record.
    # concept_ids match Athena HemOnc vocabulary (verified against staging DB).
    # ------------------------------------------------------------------
    _c(35806260, 'RVD',          'Episode', 'HemOnc', 'Regimen', 'S', 'RVD'),           # VRd
    _c(35806311, 'Dara-Rd',      'Episode', 'HemOnc', 'Regimen', 'S', 'Dara-Rd'),       # DaraRD / DRd
    _c(905602,   'Dara-KRd',     'Episode', 'HemOnc', 'Regimen', 'S', 'Dara-KRd'),      # DKRd
    _c(35806284, 'KRd',          'Episode', 'HemOnc', 'Regimen', 'S', 'KRd'),           # KRD
    _c(35806053, 'Rd',           'Episode', 'HemOnc', 'Regimen', 'S', 'Rd'),            # Rd
    _c(35806066, 'PomDex',       'Episode', 'HemOnc', 'Regimen', 'S', 'PomDex'),        # Pd
    _c(35806324, 'KPd',          'Episode', 'HemOnc', 'Regimen', 'S', 'KPd'),           # KPd
    _c(35806326, 'DaraPd',       'Episode', 'HemOnc', 'Regimen', 'S', 'DaraPd'),        # DPd
    _c(911941,   'Isa-Pd',       'Episode', 'HemOnc', 'Regimen', 'S', 'Isa-Pd'),        # IsaPd
    _c(35806313, 'Elo-Pd',       'Episode', 'HemOnc', 'Regimen', 'S', 'Elo-Pd'),        # EloPd
    _c(905768,   'SVd',          'Episode', 'HemOnc', 'Regimen', 'S', 'SVd'),           # SVd
    _c(37557075, 'Teclistamab',  'Episode', 'HemOnc', 'Regimen', 'S', 'Teclistamab'),   # Teclistamab
    _c(911956,   'Belantamab',   'Episode', 'HemOnc', 'Regimen', 'S', 'Belantamab'),    # Belantamab mafodotin
    _c(1525038,  'Cilta-cel',    'Episode', 'HemOnc', 'Regimen', 'S', 'Cilta-cel'),     # Ciltacabtagene
    _c(905696,   'Ide-cel',      'Episode', 'HemOnc', 'Regimen', 'S', 'Ide-cel'),       # Idecabtagene
    _c(911993,   'Dara-RVd',     'Episode', 'HemOnc', 'Regimen', 'S', 'Dara-RVd'),      # DaraVRd
    _c(37557069, 'Isa-RVd',      'Episode', 'HemOnc', 'Regimen', 'S', 'Isa-RVd'),       # IsaVRd
    _c(35806283, 'IRd',          'Episode', 'HemOnc', 'Regimen', 'S', 'IRd'),           # IxaRd
    _c(35806314, 'Elo-Rd',       'Episode', 'HemOnc', 'Regimen', 'S', 'Elo-Rd'),        # EloRd
    _c(35806059, 'Vd',           'Episode', 'HemOnc', 'Regimen', 'S', 'Vd'),            # Vd
    _c(35806061, 'VDC',          'Episode', 'HemOnc', 'Regimen', 'S', 'VDC'),           # VCd/CyBorD
    _c(35806312, 'Dara-Vd',      'Episode', 'HemOnc', 'Regimen', 'S', 'Dara-Vd'),       # DVd
    _c(35806309, 'Kd',           'Episode', 'HemOnc', 'Regimen', 'S', 'Kd'),            # Kd
    _c(35806268, 'TD',           'Episode', 'HemOnc', 'Regimen', 'S', 'TD'),            # Td
    _c(35806258, 'VMP',          'Episode', 'HemOnc', 'Regimen', 'S', 'VMP'),           # VMP
    _c(35806056, 'MP',           'Episode', 'HemOnc', 'Regimen', 'S', 'MP'),            # Melphalan+pred
    _c(35804011, 'Melphalan',    'Episode', 'HemOnc', 'Regimen', 'S', 'Melphalan'),     # Mel200
]


def _assert_local_mint_convention():
    """Fail fast if any seeded row breaks the local-mint quarantine rules.

    Guards the three invariants that, when violated, produced #413 and #415:
      - a locally-authored row must live in an HK-* vocabulary,
      - it must carry source='HealthKey',
      - and its concept_id must be in the OHDSI custom range (>= 2e9), which
        Athena never allocates, so it cannot collide on the primary key.
    """
    problems = []
    for row in _CONCEPTS:
        is_hk_vocab = row['vocabulary_id'].startswith('HK-')
        is_tagged = row.get('source') == 'HealthKey'
        in_local_range = row['concept_id'] >= LOCAL_CONCEPT_ID_MIN

        if is_hk_vocab or is_tagged:
            if not is_hk_vocab:
                problems.append(
                    f"{row['concept_id']} {row['concept_code']}: source='HealthKey' "
                    f"but vocabulary is {row['vocabulary_id']!r}, not HK-*")
            if not is_tagged:
                problems.append(
                    f"{row['concept_id']} {row['concept_code']}: HK-* vocabulary "
                    f"but source is {row.get('source')!r}, not 'HealthKey'")
            if not in_local_range:
                problems.append(
                    f"{row['concept_id']} {row['concept_code']}: local mint must have "
                    f"concept_id >= {LOCAL_CONCEPT_ID_MIN}")
        elif in_local_range:
            problems.append(
                f"{row['concept_id']} {row['concept_code']}: concept_id is in the local "
                f"range but the row is not marked as a HealthKey mint")

    if problems:
        raise CommandError(
            'Seed rows violate the local-mint convention:\n  '
            + '\n  '.join(problems))


class Command(BaseCommand):
    help = (
        'Seed the minimal OMOP concept rows required for mCODE FHIR import. '
        'Use load_athena_vocabularies for a full production vocabulary load.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Print what would be seeded without writing to the DB.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']

        _assert_local_mint_convention()

        if dry_run:
            self.stdout.write(self.style.WARNING('Dry-run mode — no changes will be written.\n'))

        with transaction.atomic():
            # Vocabularies
            v_created = 0
            for v in _VOCABULARIES:
                if dry_run:
                    if not Vocabulary.objects.filter(vocabulary_id=v['vocabulary_id']).exists():
                        self.stdout.write(f"  [would create vocabulary] {v['vocabulary_id']}")
                        v_created += 1
                else:
                    _, created = Vocabulary.objects.get_or_create(
                        vocabulary_id=v['vocabulary_id'], defaults=v)
                    if created:
                        v_created += 1

            # Domains
            d_created = 0
            for d in _DOMAINS:
                if dry_run:
                    if not Domain.objects.filter(domain_id=d['domain_id']).exists():
                        self.stdout.write(f"  [would create domain] {d['domain_id']}")
                        d_created += 1
                else:
                    _, created = Domain.objects.get_or_create(
                        domain_id=d['domain_id'], defaults=d)
                    if created:
                        d_created += 1

            # Concept classes
            cc_created = 0
            for cc in _CONCEPT_CLASSES:
                if dry_run:
                    if not ConceptClass.objects.filter(concept_class_id=cc['concept_class_id']).exists():
                        self.stdout.write(f"  [would create concept_class] {cc['concept_class_id']}")
                        cc_created += 1
                else:
                    _, created = ConceptClass.objects.get_or_create(
                        concept_class_id=cc['concept_class_id'], defaults=cc)
                    if created:
                        cc_created += 1

            # Concepts
            c_created = c_existing = 0
            for row in _CONCEPTS:
                if dry_run:
                    exists = Concept.objects.filter(concept_id=row['concept_id']).exists()
                    status = 'exists' if exists else 'would create'
                    self.stdout.write(
                        f"  [{status}] {row['concept_id']:>8}  {row['concept_code']:<12}  {row['concept_name']}")
                    if not exists:
                        c_created += 1
                    else:
                        c_existing += 1
                else:
                    _, created = Concept.objects.get_or_create(
                        concept_id=row['concept_id'], defaults=row)
                    if created:
                        c_created += 1
                    else:
                        c_existing += 1

            if dry_run:
                transaction.set_rollback(True)

        summary = (
            f'Vocabularies: {v_created} new  |  '
            f'Domains: {d_created} new  |  '
            f'ConceptClasses: {cc_created} new  |  '
            f'Concepts: {c_created} new, {c_existing} already present'
        )
        if dry_run:
            self.stdout.write(self.style.WARNING(f'\nDry-run summary: {summary}'))
        else:
            self.stdout.write(self.style.SUCCESS(summary))
