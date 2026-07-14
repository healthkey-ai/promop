"""
Management command: generate_synthea_bc

Generates 100 rich breast cancer FHIR R4 patient bundles using Synthea, combining
individual per-patient files and hospital/practitioner metadata into a single Bundle.

The command:
  1. Locates the Synthea JAR (auto-detects Homebrew OpenJDK; override with --java-path /
     --jar-path).
  2. Writes a custom Synthea module that forces 100 % breast-cancer incidence in all
     female patients (by removing the probabilistic age gates present in the built-in
     module).
  3. Runs Synthea to generate exactly N alive breast-cancer patients, re-running with
     a larger population if needed.
  4. Filters to alive-only patients, merges all individual FHIR files together with
     hospitalInformation and practitionerInformation, and writes one consolidated
     Bundle JSON.

The resulting bundle is suitable for import into the OMOP CDM via import_fhir_bundle
and populates:
  person, location, care_site, provider, visit_occurrence, visit_detail,
  condition_occurrence, condition_era, drug_exposure, drug_era, dose_era,
  procedure_occurrence, measurement, observation, specimen, note, note_nlp,
  device_exposure, immunization, imaging_study, payer_plan_period, observation_period,
  episode, episode_event, death, person_language_skill

Usage:
    python manage.py generate_synthea_bc
    python manage.py generate_synthea_bc --count 50 --output /data/bc_50.json
    python manage.py generate_synthea_bc --jar-path /opt/synthea/synthea.jar
    python manage.py generate_synthea_bc --java-path /usr/lib/jvm/java-17/bin/java
    python manage.py generate_synthea_bc --state California --city "Los Angeles"
    python manage.py generate_synthea_bc --seed 42 --count 100
"""

import copy
import glob
import json
import os
import re
import random
import shutil
import subprocess
import tempfile
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
_DEFAULT_COUNT = 100
_DEFAULT_STATE = 'Massachusetts'
_DEFAULT_CITY = ''
_DEFAULT_OUTPUT = '/tmp/synthea_bc_100_claude.json'

# Candidate Java binary locations (tried in order)
_JAVA_CANDIDATES = [
    '/opt/homebrew/Cellar/openjdk@17/17.0.19/libexec/openjdk.jdk/Contents/Home/bin/java',
    '/opt/homebrew/opt/openjdk@17/bin/java',
    '/opt/homebrew/opt/openjdk/bin/java',
    '/usr/lib/jvm/java-17-openjdk-amd64/bin/java',
    '/usr/lib/jvm/java-17/bin/java',
    '/usr/local/bin/java',
]

# Candidate Synthea JAR locations (tried in order)
_JAR_CANDIDATES = [
    str(Path.home() / 'synthea' / 'synthea-with-dependencies.jar'),
    '/opt/synthea/synthea-with-dependencies.jar',
    '/usr/local/lib/synthea-with-dependencies.jar',
]

# Synthea JAR download URL (latest master-branch release)
_JAR_DOWNLOAD_URL = (
    'https://github.com/synthetichealth/synthea/releases/download/'
    'master-branch-latest/synthea-with-dependencies.jar'
)


def _find_java() -> str | None:
    for path in _JAVA_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Last resort: PATH
    found = shutil.which('java')
    if found:
        return found
    return None


def _find_jar() -> str | None:
    for path in _JAR_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _make_bc_module_100pct(base_module_path: str) -> dict:
    """
    Load the standard breast_cancer.json and patch it so that:
      - All female patients develop breast cancer (probability 1.0).
      - All age-gate Delay states are removed (replaced with direct transitions)
        so the cancer onset fires immediately instead of waiting for an age window
        that may fall outside the patient's simulated lifespan.
      - Males go straight to Terminal (male BC is omitted from this cohort).

    Returns the modified module dict.
    """
    with open(base_module_path) as f:
        module = json.load(f)

    # Force 100 % incidence for females
    module['states']['Female'] = {
        'type': 'Simple',
        'remarks': ['Forced 100% incidence for synthetic cohort generation.'],
        'direct_transition': 'Pre_breastCancer',
    }

    # Males terminate immediately
    module['states']['Male'] = {
        'type': 'Simple',
        'direct_transition': 'Terminal',
    }

    # Skip the age-based routing entirely — go straight to symptom onset
    module['states']['Pre_breastCancer'] = {
        'type': 'Simple',
        'remarks': ['Age gating removed — all patients develop breast cancer.'],
        'direct_transition': 'BreastCancer_Symptom1',
    }

    # Replace all "Age X-Y" Delay states with a simple pass-through
    for state_name, state in list(module['states'].items()):
        if state_name.startswith('Age ') and state.get('type') == 'Delay':
            module['states'][state_name] = {
                'type': 'Simple',
                'direct_transition': 'BreastCancer_Symptom1',
            }

    # Prevent a screening-based shortcut that can bypass ConditionOnset
    if 'Breast Cancer Screening Due' in module['states']:
        module['states']['Breast Cancer Screening Due'] = {
            'type': 'Simple',
            'direct_transition': 'BreastCancer_Symptom1',
        }

    return module


def _has_breast_cancer(patient_bundle: dict) -> bool:
    """Return True if the FHIR patient Bundle contains a breast-cancer Condition."""
    for entry in patient_bundle.get('entry', []):
        resource = entry.get('resource', {})
        if resource.get('resourceType') != 'Condition':
            continue
        code = resource.get('code', {})
        text = code.get('text', '').lower()
        codings = code.get('coding', [])
        if 'breast' in text:
            return True
        for coding in codings:
            # SNOMED 254837009 = Malignant neoplasm of breast
            if coding.get('code') in ('254837009',):
                return True
            if 'C50' in str(coding.get('code', '')):
                return True
    return False


def _is_deceased(patient_bundle: dict) -> bool:
    for entry in patient_bundle.get('entry', []):
        resource = entry.get('resource', {})
        if resource.get('resourceType') == 'Patient':
            if 'deceasedDateTime' in resource:
                return True
            if resource.get('deceasedBoolean') is True:
                return True
    return False


_BC_STAGE_CHOICES = [
    ('I', 16),
    ('IA', 14),
    ('IB', 8),
    ('IIA', 18),
    ('IIB', 16),
    ('IIIA', 10),
    ('IIIB', 8),
    ('IIIC', 4),
    ('IV', 6),
]

_BC_STAGE_TO_TNM = {
    'I': ('T1', 'N0', 'M0'),
    'IA': ('T1', 'N0', 'M0'),
    'IB': ('T1', 'N0', 'M0'),
    'IIA': ('T2', 'N0', 'M0'),
    'IIB': ('T2', 'N1', 'M0'),
    'IIIA': ('T3', 'N1', 'M0'),
    'IIIB': ('T4', 'N2', 'M0'),
    'IIIC': ('T4', 'N3', 'M0'),
    'IV': ('T4', 'N3', 'M1'),
}

# Regimen plans for MedicationStatement generation. Each carries the HemOnc
# OMOP concept_id (None if not in the vocabulary) and a typical course length
# so the emitted MedicationStatement's effectivePeriod is clinically plausible.
_BC_REGIMEN_PLANS = {
    'early': [
        {'name': 'AC-T', 'drugs': ['doxorubicin', 'cyclophosphamide', 'paclitaxel'],
         'hemonc_id': 35101507, 'duration_days': 168},
        {'name': 'TC', 'drugs': ['docetaxel', 'cyclophosphamide'],
         'hemonc_id': 35804232, 'duration_days': 126},
        {'name': 'THP', 'drugs': ['paclitaxel', 'trastuzumab', 'pertuzumab'],
         'hemonc_id': 1525210, 'duration_days': 252},
        {'name': 'Tamoxifen monotherapy', 'drugs': ['tamoxifen'],
         'hemonc_id': 35804221, 'duration_days': 1095},
    ],
    'advanced': [
        {'name': 'AC-T', 'drugs': ['doxorubicin', 'cyclophosphamide', 'paclitaxel'],
         'hemonc_id': 35101507, 'duration_days': 168},
        {'name': 'TCH+P', 'drugs': ['trastuzumab', 'pertuzumab', 'docetaxel'],
         'hemonc_id': 35804254, 'duration_days': 252},
        {'name': 'T-DXd', 'drugs': ['trastuzumab deruxtecan'],
         'hemonc_id': 42542261, 'duration_days': 210},
        {'name': 'Palbociclib+AI', 'drugs': ['palbociclib', 'letrozole'],
         'hemonc_id': None, 'duration_days': 730},
    ],
}

_BC_2L_PLANS = [
    {'name': 'T-DXd', 'drugs': ['trastuzumab deruxtecan'],
     'hemonc_id': 42542261, 'duration_days': 210},
    {'name': 'T-DM1', 'drugs': ['ado-trastuzumab emtansine'],
     'hemonc_id': 35805230, 'duration_days': 180},
    {'name': 'Capecitabine', 'drugs': ['capecitabine'],
     'hemonc_id': 35804227, 'duration_days': 126},
    {'name': 'Olaparib', 'drugs': ['olaparib'],
     'hemonc_id': 35804269, 'duration_days': 240},
    {'name': 'SG', 'drugs': ['sacituzumab govitecan'],
     'hemonc_id': 912024, 'duration_days': 180},
]

_BC_3L_PLANS = [
    {'name': 'Eribulin', 'drugs': ['eribulin'],
     'hemonc_id': 35804265, 'duration_days': 126},
    {'name': 'Capecitabine', 'drugs': ['capecitabine'],
     'hemonc_id': 35804227, 'duration_days': 126},
    {'name': 'SG', 'drugs': ['sacituzumab govitecan'],
     'hemonc_id': 912024, 'duration_days': 180},
]

_BC_LABS = [
    ('718-7', 'Hemoglobin [Mass/volume] in Blood', 'g/dL'),
    ('4544-3', 'Hematocrit [Volume Fraction] of Blood', '%'),
    ('6690-2', 'Leukocytes [#/volume] in Blood', '10*3/uL'),
    ('789-8', 'Erythrocytes [#/volume] in Blood', '10*6/uL'),
    ('777-3', 'Platelets [#/volume] in Blood', '10*3/uL'),
    ('751-8', 'Neutrophils [#/volume] in Blood', '10*3/uL'),
    ('731-0', 'Lymphocytes [#/volume] in Blood', '10*3/uL'),
    ('742-7', 'Monocytes [#/volume] in Blood', '10*3/uL'),
    ('2160-0', 'Creatinine [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('17861-6', 'Calcium [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('2951-2', 'Sodium [Moles/volume] in Serum or Plasma', 'mEq/L'),
    ('2823-3', 'Potassium [Moles/volume] in Serum or Plasma', 'mEq/L'),
    ('2601-3', 'Magnesium [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('2777-1', 'Phosphate [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('1751-7', 'Albumin [Mass/volume] in Serum or Plasma', 'g/dL'),
    ('1975-2', 'Bilirubin.total [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('1742-6', 'Alanine aminotransferase [Enzymatic activity/volume] in Serum or Plasma', 'U/L'),
    ('1920-8', 'Aspartate aminotransferase [Enzymatic activity/volume] in Serum or Plasma', 'U/L'),
    ('6768-6', 'Alkaline phosphatase [Enzymatic activity/volume] in Serum or Plasma', 'U/L'),
    ('2885-2', 'Protein [Mass/volume] in Serum or Plasma', 'g/dL'),
    ('2345-7', 'Glucose [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('4548-4', 'Hemoglobin A1c/Hemoglobin.total in Blood', '%'),
    ('33914-3', 'Glomerular filtration rate/1.73 sq M.predicted', 'mL/min/1.73m2'),
    ('3094-0', 'Urea nitrogen [Mass/volume] in Serum or Plasma', 'mg/dL'),
    ('2532-0', 'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma', 'U/L'),
    ('1952-1', 'Beta-2 microglobulin [Mass/volume] in Serum or Plasma', 'mg/L'),
    ('2532-0', 'Lactate dehydrogenase [Enzymatic activity/volume] in Serum or Plasma', 'U/L'),
    ('55454-3', 'Carcinoembryonic Ag [Mass/volume] in Serum or Plasma', 'ng/mL'),
    ('1988-5', 'C reactive protein [Mass/volume] in Serum or Plasma', 'mg/L'),
    ('30341-2', 'Erythrocyte sedimentation rate', 'mm/h'),
    ('6301-6', 'INR in Platelet poor plasma', '{INR}'),
    ('5902-2', 'Prothrombin time (PT)', 's'),
    ('3173-2', 'aPTT in Platelet poor plasma', 's'),
    ('8302-2', 'Body height', 'cm'),
    ('29463-7', 'Body weight', 'kg'),
    ('89247-1', 'ECOG Performance Status score', '{score}'),
    ('89243-0', 'Karnofsky Performance Status score', '{score}'),
]

_BC_BIOMARKERS = [
    ('85337-4', 'Ki-67 [Interpretation] in Tissue', None),
    ('16112-5', 'Estrogen receptor [Interpretation] in Tissue', 'positive'),
    ('16113-3', 'Progesterone receptor [Interpretation] in Tissue', 'positive'),
    ('48676-1', 'HER2 [Interpretation] in Tissue', 'negative'),
    ('85319-2', 'Ki-67 Ag [Presence] in Tissue by Immune stain', None),
    ('85336-6', 'PD-L1 immune cells [#/area] in Tissue by Immune stain', None),
    ('96893-3', 'PD-L1 combined positive score', None),
    ('44648-4', 'Biopsy grade', None),
    ('76690-7', 'Menopausal status', None),
    ('44667-4', 'Bone-only metastasis status', None),
]

_BC_BIOMARKER_DISPLAYS = {code: display for code, display, _ in _BC_BIOMARKERS}

_BC_HISTOLOGY_TYPES = [
    'Invasive ductal carcinoma of breast',
    'Invasive lobular carcinoma of breast',
    'Breast carcinoma, NOS',
]

_BC_BEHAVIOR_OBS = [
    ('266919005', 'Never smoked tobacco'),
    ('8517006', 'Ex-smoker'),
    ('77176002', 'Current smoker'),
]

_BC_BEHAVIOR_DISPLAYS = {code: display for code, display in _BC_BEHAVIOR_OBS}

_BC_BEHAVIOR_LABS = [
    ('72166-2', 'Smoking Status'),
    ('63640-7', 'Pack Years'),
    ('74013-4', 'Alcohol Use'),
    ('11286-7', 'Drinks per Week'),
    ('68516-4', 'Exercise Frequency'),
    ('89555-7', 'Exercise Minutes per Week'),
    ('88365-2', 'Diet Type'),
    ('93831-6', 'Sleep Quality'),
    ('73985-4', 'Stress Level'),
    ('93033-9', 'Social Support'),
    ('74165-2', 'Employment Status'),
    ('82589-3', 'Education Level'),
    ('45404-1', 'Marital Status'),
    ('76513-1', 'Insurance Type'),
    ('63512-8', 'Number of Dependents'),
    ('77243-3', 'Annual Household Income'),
    ('75985-6', 'Ability to Consent'),
    ('74014-2', 'Caregiver Availability'),
    ('8659-8', 'Contraceptive Use'),
    ('2106-3', 'Pregnancy Test'),
    ('75618-3', 'Mental Health Disorders'),
    ('74204-0', 'Non-prescription Drug Use'),
    ('82593-5', 'Geographic/Environmental Exposure Risk'),
]

_BC_MUTATION_LOINCS = [
    ('21636-6', 'BRCA1'),
    ('21637-4', 'BRCA2'),
    ('21667-1', 'TP53'),
    ('62318-1', 'PIK3CA'),
]

_BC_RESPONSE_CODES = [
    ('182840001', 'Complete Response'),
    ('182841002', 'Partial Response'),
    ('182843004', 'Stable Disease'),
    ('182842009', 'Progressive Disease'),
]

_BC_WEARABLE_RANGES = {
    'steps': (2000, 12000),
    'active_minutes': (0, 90),
    'resting_hr': (55, 90),
    'hrv_sdnn': (20, 80),
    'spo2': (94.0, 99.0),
    'respiratory_rate': (12, 20),
    'sleep_duration': (5.5, 8.5),
}

_BC_WEARABLE_CODES = {
    'steps': '55423-8',
    'active_minutes': '77592-4',
    'resting_hr': '40443-4',
    'hrv_sdnn': '80404-7',
    'spo2': '59408-5',
    'respiratory_rate': '9279-1',
    'sleep_duration': '93832-4',
}

_BC_WEARABLE_UNITS = {
    'steps': 'steps',
    'active_minutes': 'min',
    'resting_hr': '/min',
    'hrv_sdnn': 'ms',
    'spo2': '%',
    'respiratory_rate': '/min',
    'sleep_duration': 'h',
}

_BC_WEARABLE_DAYS = 30


def _clean_name_piece(value: str) -> str:
    if not value:
        return ''
    cleaned = re.sub(r'\d+', '', value).strip()
    return cleaned or value


def _iso_date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat().replace('+00:00', 'Z')


def _choose_bc_stage(rng: random.Random, deceased: bool) -> str:
    stages = [stage for stage, _ in _BC_STAGE_CHOICES]
    weights = []
    for stage, weight in _BC_STAGE_CHOICES:
        adjusted = weight
        if deceased and stage in {'IIIA', 'IIIB', 'IIIC', 'IV'}:
            adjusted += 4
        elif not deceased and stage in {'I', 'IA', 'IB', 'IIA'}:
            adjusted += 4
        weights.append(adjusted)
    return rng.choices(stages, weights=weights, k=1)[0]


def _make_bc_condition(patient_ref: str, onset: datetime, stage: str, histology: str) -> dict:
    return {
        'resourceType': 'Condition',
        'id': str(uuid.uuid4()),
        'clinicalStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical',
                'code': 'active',
                'display': 'Active',
            }],
        },
        'verificationStatus': {
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
                'code': 'confirmed',
                'display': 'Confirmed',
            }],
        },
        'category': [{
            'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-category',
                'code': 'problem-list-item',
                'display': 'Problem List Item',
            }],
        }],
        'code': {
            'coding': [{
                'system': 'http://snomed.info/sct',
                'code': '254837009',
                'display': histology,
            }],
            'text': histology,
        },
        'subject': {'reference': patient_ref},
        'onsetDateTime': _iso_date(onset),
        'stage': [{'summary': {'text': f'Breast Cancer Stage {stage}'}}],
    }


def _make_stage_observation(patient_ref: str, onset: datetime, code: str, value: str) -> dict:
    display = {
        '21908-9': 'Stage group.clinical Cancer',
        '21905-5': 'Tumor stage.clinical Cancer',
        '21906-3': 'Nodes stage.clinical Cancer',
        '21901-4': 'Distant metastases.clinical Cancer',
    }.get(code, 'Breast cancer staging')
    return {
        'resourceType': 'Observation',
        'id': str(uuid.uuid4()),
        'status': 'final',
        'code': {
            'coding': [{
                'system': 'http://loinc.org',
                'code': code,
                'display': display,
            }],
            'text': display,
        },
        'subject': {'reference': patient_ref},
        'effectiveDateTime': _iso_date(onset),
        'valueCodeableConcept': {'text': value},
    }


def _make_bc_diagnostic_report(patient_ref: str, onset: datetime, stage: str) -> dict:
    return {
        'resourceType': 'DiagnosticReport',
        'id': str(uuid.uuid4()),
        'status': 'final',
        'code': {
            'coding': [{
                'system': 'http://loinc.org',
                'code': '11502-2',
                'display': 'Pathology study',
            }],
            'text': 'Pathology study',
        },
        'subject': {'reference': patient_ref},
        'effectiveDateTime': _iso_date(onset),
        'conclusion': f'Breast cancer diagnostic report. Stage {stage}.',
    }


def _make_therapy_medication_statement(
    patient_ref: str,
    lot_num: int,
    regimen_name: str,
    hemonc_id: int | None,
    drugs: list,
    start_dt: datetime,
    end_dt: datetime,
    outcome: str,
) -> list:
    """Return MedicationStatement resources for one therapy line.

    The first resource is the regimen-level statement carrying 'therapy-line'
    and 'therapy-outcome' extensions plus a HemOnc coding — the FHIR import
    handler reads these to create an Episode (via the shared LOT writer) and a
    LOT-{n}-outcome Observation. Subsequent resources are individual drug
    sub-statements with a 'partOf' reference back to the regimen statement.
    """
    regimen_id = str(uuid.uuid4())
    coding = [{
        'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
        'code': regimen_name,
        'display': regimen_name,
    }]
    if hemonc_id:
        coding.append({
            'system': 'http://ohdsi.org/omop/HemOnc',
            'code': str(hemonc_id),
            'display': regimen_name,
        })

    resources = [{
        'resourceType': 'MedicationStatement',
        'id': regimen_id,
        'status': 'completed',
        'extension': [
            {'url': 'http://hl7.org/fhir/StructureDefinition/therapy-line',
             'valueInteger': lot_num},
            {'url': 'http://hl7.org/fhir/StructureDefinition/therapy-outcome',
             'valueString': outcome},
        ],
        'medicationCodeableConcept': {'coding': coding, 'text': regimen_name},
        'subject': {'reference': patient_ref},
        'effectivePeriod': {'start': _iso_date(start_dt), 'end': _iso_date(end_dt)},
    }]

    for drug in drugs:
        resources.append({
            'resourceType': 'MedicationStatement',
            'id': str(uuid.uuid4()),
            'status': 'completed',
            'partOf': [{'reference': f'urn:uuid:{regimen_id}'}],
            'extension': [
                {'url': 'http://hl7.org/fhir/StructureDefinition/therapy-line',
                 'valueInteger': lot_num},
            ],
            'medicationCodeableConcept': {
                'coding': [{
                    'system': 'http://www.nlm.nih.gov/research/umls/rxnorm',
                    'code': drug,
                    'display': drug.title(),
                }],
                'text': drug.title(),
            },
            'subject': {'reference': patient_ref},
            'effectivePeriod': {'start': _iso_date(start_dt), 'end': _iso_date(end_dt)},
        })

    return resources


def _make_lab_observation(patient_ref: str, onset: datetime, code: str, display: str,
                          value, unit: str | None = None) -> dict:
    obs = {
        'resourceType': 'Observation',
        'id': str(uuid.uuid4()),
        'status': 'final',
        'code': {
            'coding': [{
                'system': 'http://loinc.org',
                'code': code,
                'display': display,
            }],
            'text': display,
        },
        'subject': {'reference': patient_ref},
        'effectiveDateTime': _iso_date(onset),
    }
    if isinstance(value, bool):
        obs['valueBoolean'] = value
    elif isinstance(value, str):
        obs['valueCodeableConcept'] = {'text': value}
    else:
        obs['valueQuantity'] = {'value': value}
        if unit:
            obs['valueQuantity']['unit'] = unit
    return obs


def _make_wearable_observation(patient_ref: str, when: datetime, code: str, display: str,
                               value, unit: str | None = None) -> dict:
    return _make_lab_observation(patient_ref, when, code, display, value, unit)


def _make_codeable_observation(patient_ref: str, onset: datetime, code: str, display: str,
                               value_text: str) -> dict:
    return {
        'resourceType': 'Observation',
        'id': str(uuid.uuid4()),
        'status': 'final',
        'code': {
            'coding': [{
                'system': 'http://snomed.info/sct',
                'code': code,
                'display': display,
            }],
            'text': display,
        },
        'subject': {'reference': patient_ref},
        'effectiveDateTime': _iso_date(onset),
        'valueCodeableConcept': {'text': value_text},
    }


def _make_procedure(patient_ref: str, onset: datetime, text: str) -> dict:
    return {
        'resourceType': 'Procedure',
        'id': str(uuid.uuid4()),
        'status': 'completed',
        'code': {'text': text},
        'subject': {'reference': patient_ref},
        'performedDateTime': _iso_date(onset),
    }


def _enrich_patient_bundle(bundle: dict, index: int) -> None:
    patient = None
    resources = []
    for entry in bundle.get('entry', []):
        resource = entry.get('resource', {})
        resources.append(resource)
        if resource.get('resourceType') == 'Patient' and patient is None:
            patient = resource
    if not patient:
        return

    names = patient.get('name') or []
    if names:
        official = names[0]
        given = [_clean_name_piece(v) for v in official.get('given', []) if v]
        family = _clean_name_piece(official.get('family', ''))
        if given:
            official['given'] = given
        if family:
            official['family'] = family
        official['use'] = official.get('use') or 'official'
        patient['name'] = [official]

    patient_ref = f"urn:uuid:{patient.get('id', '')}"
    if not patient_ref.strip('urn:uuid:'):
        return

    rng = random.Random(f"{patient.get('id', index)}:{index}")
    deceased = _is_deceased(bundle)

    onset_candidates = []
    for entry in bundle.get('entry', []):
        resource = entry.get('resource', {})
        if resource.get('resourceType') != 'Encounter':
            continue
        start = (resource.get('period') or {}).get('start')
        if not start:
            continue
        try:
            onset_candidates.append(datetime.fromisoformat(start.replace('Z', '+00:00')))
        except ValueError:
            continue
    onset = min(onset_candidates) if onset_candidates else None
    if onset is None:
        birth = patient.get('birthDate')
        if birth:
            try:
                onset = datetime.fromisoformat(birth).replace(tzinfo=timezone.utc) + timedelta(days=365 * rng.randint(44, 68))
            except ValueError:
                onset = datetime.now(timezone.utc) - timedelta(days=rng.randint(365, 365 * 8))
        else:
            onset = datetime.now(timezone.utc) - timedelta(days=rng.randint(365, 365 * 8))

    stage = _choose_bc_stage(rng, deceased)
    stage_t, stage_n, stage_m = _BC_STAGE_TO_TNM[stage]
    stage_label = f'Stage {stage}'
    histology = rng.choice(_BC_HISTOLOGY_TYPES)

    breast_conditions = []
    for resource in resources:
        if resource.get('resourceType') != 'Condition':
            continue
        code = resource.get('code', {})
        text = ' '.join(
            filter(
                None,
                [code.get('text', ''), *[c.get('display', '') for c in code.get('coding', [])]],
            )
        ).lower()
        if 'breast' in text or any(c.get('code') == '254837009' for c in code.get('coding', [])):
            breast_conditions.append(resource)

    if breast_conditions:
        for condition in breast_conditions:
            if not condition.get('stage'):
                condition['stage'] = [{'summary': {'text': f'Breast Cancer Stage {stage}'}}]
            if not condition.get('onsetDateTime'):
                condition['onsetDateTime'] = _iso_date(onset)
            code = condition.setdefault('code', {})
            code.setdefault('text', histology)
            if not code.get('coding'):
                code['coding'] = [{
                    'system': 'http://snomed.info/sct',
                    'code': '254837009',
                    'display': histology,
                }]
    else:
        bundle.setdefault('entry', []).append({'resource': _make_bc_condition(patient_ref, onset, stage, histology)})

    existing_stage_codes = {
        ((resource.get('code') or {}).get('coding') or [{}])[0].get('code')
        for resource in resources
        if resource.get('resourceType') == 'Observation'
    } & {'21908-9', '21905-5', '21906-3', '21901-4'}
    for code, value in {
        '21908-9': stage_label,
        '21905-5': stage_t,
        '21906-3': stage_n,
        '21901-4': stage_m,
    }.items():
        if code not in existing_stage_codes:
            bundle.setdefault('entry', []).append({
                'resource': _make_stage_observation(patient_ref, onset, code, value),
            })

    has_breast_report = any(
        resource.get('resourceType') == 'DiagnosticReport'
        and resource.get('subject', {}).get('reference') == patient_ref
        and 'breast' in json.dumps(resource).lower()
        for resource in resources
    )
    if not has_breast_report:
        bundle.setdefault('entry', []).append({
            'resource': _make_bc_diagnostic_report(patient_ref, onset, stage),
        })

    # Receptor status and biomarker observations need to exist as OMOP
    # Measurements/Observations with the exact LOINC codes PatientRecord reads.
    receptor_pattern = {
        '16112-5': 'positive' if stage not in {'IV'} else 'negative',
        '16113-3': 'positive' if stage not in {'IV'} else 'negative',
        '48676-1': 'negative' if stage in {'I', 'IA', 'IB', 'IIA'} else 'positive',
    }
    existing_receptor_codes = {
        ((resource.get('code') or {}).get('coding') or [{}])[0].get('code')
        for resource in resources
        if resource.get('resourceType') == 'Observation'
    } & set(receptor_pattern)
    for code, status in receptor_pattern.items():
        if code not in existing_receptor_codes:
            display = {
                '16112-5': 'Estrogen receptor [Interpretation] in Tissue',
                '16113-3': 'Progesterone receptor [Interpretation] in Tissue',
                '48676-1': 'HER2 [Interpretation] in Tissue',
            }[code]
            bundle.setdefault('entry', []).append({
                'resource': _make_codeable_observation(patient_ref, onset, code, display, status),
            })

    biomarker_values = {
        '85337-4': (rng.randint(5, 95), '%'),
        '85319-2': (rng.randint(5, 75), '%'),
        '85336-6': (rng.randint(1, 30), '%'),
        '96893-3': (rng.randint(1, 50), None),
        '44648-4': (rng.choice([1, 2, 3]), None),
        '76690-7': (rng.choice(['Premenopausal', 'Perimenopausal', 'Postmenopausal']), None),
        '44667-4': (rng.choice([True, False]), None),
    }
    existing_biomarker_codes = {
        ((resource.get('code') or {}).get('coding') or [{}])[0].get('code')
        for resource in resources
        if resource.get('resourceType') == 'Observation'
    } & set(biomarker_values)
    for code, value in biomarker_values.items():
        if code in existing_biomarker_codes:
            continue
        display = _BC_BIOMARKER_DISPLAYS.get(code, code)
        bundle.setdefault('entry', []).append({
            'resource': _make_lab_observation(patient_ref, onset, code, display, value[0], value[1]),
        })

    behavior_values = {
        '72166-2': rng.choice(['Never smoked tobacco', 'Ex-smoker', 'Current smoker']),
        '63640-7': rng.randint(0, 40),
        '74013-4': rng.choice(['No alcohol use', 'Occasional alcohol use', 'Heavy alcohol use']),
        '11286-7': rng.randint(0, 21),
        '68516-4': rng.choice(['Never', 'Sometimes', 'Weekly', 'Daily']),
        '89555-7': rng.randint(0, 300),
        '88365-2': rng.choice(['Balanced diet', 'Low carb', 'Mediterranean', 'Standard']),
        '93831-6': rng.choice(['Good', 'Fair', 'Poor']),
        '73985-4': rng.choice(['Low', 'Moderate', 'High']),
        '93033-9': rng.choice(['Strong', 'Moderate', 'Limited']),
        '74165-2': rng.choice(['Employed', 'Unemployed', 'Retired']),
        '82589-3': rng.choice(['High school', 'College', 'Graduate degree']),
        '45404-1': rng.choice(['Single', 'Married', 'Divorced', 'Widowed']),
        '76513-1': rng.choice(['Commercial', 'Medicaid', 'Medicare', 'Self-pay']),
        '63512-8': rng.randint(0, 6),
        '77243-3': rng.randint(25000, 220000),
        '75985-6': rng.choice(['Yes', 'No']),
        '74014-2': rng.choice(['Yes', 'No']),
        '8659-8': rng.choice(['Yes', 'No']),
        '2106-3': rng.choice(['Negative', 'Positive', 'Not applicable']),
        '75618-3': rng.choice(['No', 'Yes']),
        '74204-0': rng.choice(['No', 'Yes']),
        '82593-5': rng.choice(['No', 'Yes']),
    }
    for code, display in _BC_BEHAVIOR_LABS:
        bundle.setdefault('entry', []).append({
            'resource': _make_lab_observation(
                patient_ref,
                onset,
                code,
                display,
                behavior_values[code],
                None,
            ),
        })

    bundle.setdefault('entry', []).append({
        'resource': _make_lab_observation(
            patient_ref,
            onset,
            '59847-4',
            'Histologic type',
            histology,
            None,
        ),
    })

    if rng.random() < 0.65:
        mutation_code, gene_name = rng.choice(_BC_MUTATION_LOINCS)
        mutation_variant = rng.choice([
            f'{gene_name} pathogenic variant',
            f'{gene_name} exon deletion',
            f'{gene_name} missense variant',
        ])
        bundle.setdefault('entry', []).append({
            'resource': _make_lab_observation(
                patient_ref,
                onset,
                mutation_code,
                f'{gene_name} mutation',
                mutation_variant,
                None,
            ),
        })

    # Behavioral assessments and best response populate PatientRecord's
    # tobacco / response fields.
    tobacco_code = rng.choice([code for code, _ in _BC_BEHAVIOR_OBS])
    if tobacco_code not in {
        ((resource.get('code') or {}).get('coding') or [{}])[0].get('code')
        for resource in resources
        if resource.get('resourceType') == 'Observation'
    }:
        tobacco_display = _BC_BEHAVIOR_DISPLAYS[tobacco_code]
        bundle.setdefault('entry', []).append({
            'resource': _make_codeable_observation(patient_ref, onset, tobacco_code, tobacco_display, tobacco_display),
        })

    response_code, response_display = rng.choice(_BC_RESPONSE_CODES)
    if response_code not in {
        ((resource.get('code') or {}).get('coding') or [{}])[0].get('code')
        for resource in resources
        if resource.get('resourceType') == 'Observation'
    }:
        bundle.setdefault('entry', []).append({
            'resource': _make_codeable_observation(patient_ref, onset, response_code, response_display, response_display),
        })

    # Therapy: multi-line MedicationStatement resources with therapy-line and
    # therapy-outcome extensions so import_fhir_bundle builds an Episode (and a
    # LOT-{n}-outcome Observation) per line via the shared LOT writer.
    therapy_bucket = 'advanced' if stage in {'IIIA', 'IIIB', 'IIIC', 'IV'} or deceased else 'early'
    lot1_plan = rng.choice(_BC_REGIMEN_PLANS[therapy_bucket])
    lot1_start = onset
    lot1_end = onset + timedelta(days=lot1_plan['duration_days'])
    lot1_outcome = rng.choices(
        [name for _, name in _BC_RESPONSE_CODES], weights=[0.25, 0.35, 0.25, 0.15], k=1,
    )[0]
    for stmt in _make_therapy_medication_statement(
        patient_ref, 1, lot1_plan['name'], lot1_plan['hemonc_id'],
        lot1_plan['drugs'], lot1_start, lot1_end, lot1_outcome,
    ):
        bundle.setdefault('entry', []).append({'resource': stmt})

    # ~30% of patients advance to a second line; of those, ~50% to a third.
    if rng.random() < 0.30:
        lot2_plan = rng.choice(_BC_2L_PLANS)
        lot2_start = lot1_end + timedelta(days=rng.randint(30, 60))
        lot2_end = lot2_start + timedelta(days=lot2_plan['duration_days'])
        lot2_outcome = rng.choices(
            [name for _, name in _BC_RESPONSE_CODES], weights=[0.15, 0.25, 0.30, 0.30], k=1,
        )[0]
        for stmt in _make_therapy_medication_statement(
            patient_ref, 2, lot2_plan['name'], lot2_plan['hemonc_id'],
            lot2_plan['drugs'], lot2_start, lot2_end, lot2_outcome,
        ):
            bundle.setdefault('entry', []).append({'resource': stmt})

        if rng.random() < 0.50:
            lot3_plan = rng.choice(_BC_3L_PLANS)
            lot3_start = lot2_end + timedelta(days=rng.randint(30, 60))
            lot3_end = lot3_start + timedelta(days=lot3_plan['duration_days'])
            lot3_outcome = rng.choices(
                [name for _, name in _BC_RESPONSE_CODES], weights=[0.05, 0.15, 0.30, 0.50], k=1,
            )[0]
            for stmt in _make_therapy_medication_statement(
                patient_ref, 3, lot3_plan['name'], lot3_plan['hemonc_id'],
                lot3_plan['drugs'], lot3_start, lot3_end, lot3_outcome,
            ):
                bundle.setdefault('entry', []).append({'resource': stmt})

    # Procedures: a simple surgical / local-control event for every patient.
    procedure = 'mastectomy' if stage in {'IIIB', 'IIIC', 'IV'} else 'lumpectomy'
    bundle.setdefault('entry', []).append({
        'resource': _make_procedure(patient_ref, onset - timedelta(days=7), procedure),
    })

    # Labs and biomarkers: create a near-complete oncology panel so the
    # resulting OMOP rows support meaningful benchmarking.
    lab_values = {
        '718-7': round(rng.uniform(8.5, 13.8), 1),
        '4544-3': round(rng.uniform(25.0, 45.0), 1),
        '6690-2': round(rng.uniform(2.5, 9.0), 1),
        '789-8': round(rng.uniform(3.0, 5.5), 1),
        '777-3': round(rng.uniform(120, 420), 0),
        '751-8': round(rng.uniform(1.2, 7.0), 1),
        '731-0': round(rng.uniform(0.8, 4.5), 1),
        '742-7': round(rng.uniform(0.1, 1.0), 1),
        '2160-0': round(rng.uniform(0.6, 1.6), 2),
        '17861-6': round(rng.uniform(8.2, 10.4), 1),
        '2951-2': round(rng.uniform(135, 145), 0),
        '2823-3': round(rng.uniform(3.5, 5.2), 1),
        '2601-3': round(rng.uniform(1.6, 2.5), 1),
        '2777-1': round(rng.uniform(2.5, 4.5), 1),
        '1751-7': round(rng.uniform(2.8, 4.8), 1),
        '1975-2': round(rng.uniform(0.1, 1.5), 1),
        '1742-6': round(rng.uniform(10, 60), 0),
        '1920-8': round(rng.uniform(10, 60), 0),
        '6768-6': round(rng.uniform(50, 180), 0),
        '2885-2': round(rng.uniform(6.0, 8.5), 1),
        '2345-7': round(rng.uniform(70, 180), 0),
        '4548-4': round(rng.uniform(4.8, 8.0), 1),
        '33914-3': round(rng.uniform(45, 110), 0),
        '3094-0': round(rng.uniform(7, 25), 0),
        '2532-0': round(rng.uniform(120, 350), 0),
        '1952-1': round(rng.uniform(1.3, 4.0), 1),
        '55454-3': round(rng.uniform(0.2, 8.0), 1),
        '1988-5': round(rng.uniform(1, 30), 1),
        '30341-2': round(rng.uniform(1, 40), 1),
        '6301-6': round(rng.uniform(0.8, 1.8), 1),
        '5902-2': round(rng.uniform(10, 20), 1),
        '3173-2': round(rng.uniform(20, 45), 1),
        '8302-2': round(rng.uniform(150, 180), 0),
        '29463-7': round(rng.uniform(50, 95), 1),
        '89247-1': rng.choice([0, 1, 2]),
        '89243-0': rng.choice([70, 80, 90, 100]),
    }
    for code, display, unit in _BC_LABS:
        if code not in lab_values:
            continue
        bundle.setdefault('entry', []).append({
            'resource': _make_lab_observation(patient_ref, onset, code, display, lab_values[code], unit if unit != '{score}' else None),
        })

    # Wearable daily summaries over the last 30 days before diagnosis.
    wearable_start = onset - timedelta(days=_BC_WEARABLE_DAYS - 1)
    for day_offset in range(_BC_WEARABLE_DAYS):
        sample_dt = wearable_start + timedelta(days=day_offset)
        day_rng = random.Random(f"{patient.get('id', index)}:{index}:{day_offset}:wearable")
        for metric_key, (lo, hi) in _BC_WEARABLE_RANGES.items():
            loinc_code = _BC_WEARABLE_CODES[metric_key]
            if metric_key in {'hrv_sdnn', 'spo2'} or isinstance(lo, float) or isinstance(hi, float):
                value = round(day_rng.uniform(lo, hi), 1)
            else:
                value = day_rng.randint(lo, hi)
            display = {
                'steps': 'Number of steps in 24 hours',
                'active_minutes': 'Moderate-vigorous physical activity duration',
                'resting_hr': 'Heart rate -- resting',
                'hrv_sdnn': 'Heart rate variability SDNN',
                'spo2': 'Oxygen saturation by pulse oximetry',
                'respiratory_rate': 'Respiratory rate',
                'sleep_duration': 'Sleep duration',
            }[metric_key]
            bundle.setdefault('entry', []).append({
                'resource': _make_wearable_observation(
                    patient_ref,
                    sample_dt,
                    loinc_code,
                    display,
                    value,
                    _BC_WEARABLE_UNITS[metric_key],
                ),
            })


def _run_synthea(
    java: str,
    jar: str,
    count: int,
    state: str,
    city: str,
    seed: int | None,
    age_range: str,
    modules_dir: str,
    output_dir: str,
    stdout_callback,
) -> None:
    """Invoke Synthea as a subprocess."""
    cmd = [
        java, '-jar', jar,
        '-p', str(count),
        '-g', 'F',
        '-a', age_range,
        '-d', modules_dir,
        f'--exporter.baseDirectory={output_dir}',
        '--exporter.fhir.export=true',
        '--exporter.csv.export=false',
        '--exporter.ccda.export=false',
        '--exporter.text.export=false',
        '--exporter.fhir_stu3.export=false',
        '--exporter.fhir_dstu2.export=false',
        '--exporter.fhir.transaction_bundle=false',
    ]
    if seed is not None:
        cmd += ['-s', str(seed)]
    if state:
        cmd.append(state)
    if city:
        cmd.append(city)

    stdout_callback(f'Running: {" ".join(cmd)}')
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        stdout_callback(result.stdout.strip())
    if result.returncode != 0:
        raise CommandError(
            f'Synthea exited with code {result.returncode}.\n'
            f'stderr: {result.stderr[:2000]}'
        )


def _collect_patient_bundles(fhir_dir: str) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Return (patient_bundles, hospital_entries, practitioner_entries) from a
    Synthea fhir/ output directory.
    """
    patient_bundles = []
    hospital_entries = []
    practitioner_entries = []

    for path in sorted(glob.glob(os.path.join(fhir_dir, '*.json'))):
        basename = os.path.basename(path)
        with open(path) as f:
            bundle = json.load(f)
        if 'hospital' in basename.lower():
            hospital_entries.extend(bundle.get('entry', []))
        elif 'practitioner' in basename.lower():
            practitioner_entries.extend(bundle.get('entry', []))
        else:
            patient_bundles.append(bundle)

    return patient_bundles, hospital_entries, practitioner_entries


class Command(BaseCommand):
    help = (
        'Generate 100 rich breast-cancer FHIR R4 patient bundles via Synthea and '
        'combine them into a single Bundle JSON file.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--count',
            type=int,
            default=_DEFAULT_COUNT,
            help=f'Number of alive breast-cancer patients to include (default: {_DEFAULT_COUNT}).',
        )
        parser.add_argument(
            '--output',
            default=_DEFAULT_OUTPUT,
            help=f'Output path for the combined FHIR Bundle JSON (default: {_DEFAULT_OUTPUT}).',
        )
        parser.add_argument(
            '--state',
            default=_DEFAULT_STATE,
            help=f'US state for Synthea demographics (default: {_DEFAULT_STATE}).',
        )
        parser.add_argument(
            '--city',
            default=_DEFAULT_CITY,
            help='City within the state (optional).',
        )
        parser.add_argument(
            '--age-range',
            default='40-80',
            help='Age range for generated patients as "min-max" (default: 40-80).',
        )
        parser.add_argument(
            '--seed',
            type=int,
            default=None,
            help='Random seed for Synthea (for reproducible output).',
        )
        parser.add_argument(
            '--java-path',
            default=None,
            help='Path to the java binary (auto-detected if omitted).',
        )
        parser.add_argument(
            '--jar-path',
            default=None,
            help='Path to synthea-with-dependencies.jar (auto-detected if omitted).',
        )
        parser.add_argument(
            '--deceased-fraction',
            type=float,
            default=0.20,
            help=(
                'Fraction of the output cohort that should be deceased patients '
                '(default: 0.20, i.e. 20 deceased out of 100). Set to 0 to exclude '
                'deceased patients entirely.'
            ),
        )
        parser.add_argument(
            '--keep-modules-dir',
            default=None,
            help=(
                'Directory to persist the generated Synthea custom modules '
                '(default: uses a temp dir that is deleted after the run).'
            ),
        )

    def handle(self, *args, **options):
        count = options['count']
        output = options['output']
        state = options['state']
        city = options['city']
        age_range = options['age_range']
        seed = options['seed']
        deceased_fraction = max(0.0, min(1.0, options['deceased_fraction']))
        n_deceased_target = round(count * deceased_fraction)
        n_alive_target = count - n_deceased_target

        # ------------------------------------------------------------------ #
        # 1. Locate java + jar
        # ------------------------------------------------------------------ #
        java = options['java_path'] or _find_java()
        if not java:
            raise CommandError(
                'Cannot find a java binary. Install OpenJDK 17+ (e.g. '
                '`brew install openjdk@17`) or pass --java-path.'
            )
        self.stdout.write(f'Using Java: {java}')

        jar = options['jar_path'] or _find_jar()
        if not jar:
            raise CommandError(
                f'Cannot find synthea-with-dependencies.jar.\n'
                f'Download it from:\n  {_JAR_DOWNLOAD_URL}\n'
                f'and place it at: {_JAR_CANDIDATES[0]}\n'
                f'or pass --jar-path.'
            )
        self.stdout.write(f'Using Synthea JAR: {jar}')

        # ------------------------------------------------------------------ #
        # 2. Extract base breast_cancer module from the JAR
        # ------------------------------------------------------------------ #
        modules_dir = options['keep_modules_dir']
        _cleanup_modules_dir = modules_dir is None
        if _cleanup_modules_dir:
            modules_dir = tempfile.mkdtemp(prefix='synthea_bc_modules_')
        else:
            os.makedirs(modules_dir, exist_ok=True)

        # Extract breast_cancer.json from the JAR into a temp directory, patch it,
        # and write the result to the custom modules directory.
        with tempfile.TemporaryDirectory() as jar_extract_dir:
            extract_result = subprocess.run(
                [
                    java.replace('java', 'jar').replace('/bin/jar', '/bin/jar')
                    if '/bin/java' in java
                    else 'jar',
                    '-xf', jar, 'modules/breast_cancer.json',
                ],
                cwd=jar_extract_dir,
                capture_output=True,
                text=True,
            )
            base_module_path = os.path.join(jar_extract_dir, 'modules', 'breast_cancer.json')
            if not os.path.exists(base_module_path):
                # Try via Python's zipfile (JAR is a ZIP)
                import zipfile
                with zipfile.ZipFile(jar) as zf:
                    zf.extract('modules/breast_cancer.json', jar_extract_dir)

            patched = _make_bc_module_100pct(base_module_path)
            custom_module_path = os.path.join(modules_dir, 'breast_cancer.json')
            with open(custom_module_path, 'w') as f:
                json.dump(patched, f, indent=2)

        self.stdout.write(
            f'Custom breast_cancer module written to {custom_module_path} '
            f'(100% incidence, age gates removed).'
        )

        # ------------------------------------------------------------------ #
        # 3. Run Synthea — retry with larger population if not enough BC patients
        # ------------------------------------------------------------------ #
        # Synthea generates `count` *alive* patients but not all may reach the
        # cancer-onset state if they die very young; add a 20 % buffer.
        synthea_count = int(count * 1.2)
        max_attempts = 3

        output_dir = tempfile.mkdtemp(prefix='synthea_bc_output_')
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            fhir_dir = os.path.join(output_dir, 'fhir')
            if os.path.isdir(fhir_dir):
                shutil.rmtree(fhir_dir)

            self.stdout.write(
                f'[Attempt {attempt}/{max_attempts}] Running Synthea with '
                f'-p {synthea_count} -g F -a {age_range} ...'
            )
            _run_synthea(
                java=java,
                jar=jar,
                count=synthea_count,
                state=state,
                city=city,
                seed=seed,
                age_range=age_range,
                modules_dir=modules_dir,
                output_dir=output_dir,
                stdout_callback=lambda m: self.stdout.write(f'  synthea: {m}'),
            )

            patient_bundles, hospital_entries, practitioner_entries = _collect_patient_bundles(fhir_dir)
            bc_alive = [b for b in patient_bundles if _has_breast_cancer(b) and not _is_deceased(b)]
            bc_deceased = [b for b in patient_bundles if _has_breast_cancer(b) and _is_deceased(b)]
            self.stdout.write(
                f'  Generated {len(patient_bundles)} patient files; '
                f'{len(bc_alive)} alive BC + {len(bc_deceased)} deceased BC patients.'
            )

            if len(bc_alive) >= n_alive_target and len(bc_deceased) >= n_deceased_target:
                break
            synthea_count = int(synthea_count * 1.5)
            self.stdout.write(
                f'  Not enough BC patients. Retrying with -p {synthea_count}...'
            )

        if len(bc_alive) < n_alive_target:
            self.stdout.write(self.style.WARNING(
                f'Only {len(bc_alive)} alive BC patients generated (wanted {n_alive_target}).'
            ))
        if len(bc_deceased) < n_deceased_target:
            self.stdout.write(self.style.WARNING(
                f'Only {len(bc_deceased)} deceased BC patients generated (wanted {n_deceased_target}).'
            ))

        # ------------------------------------------------------------------ #
        # 4. Combine into single Bundle (alive + deceased slice)
        # ------------------------------------------------------------------ #
        selected = [copy.deepcopy(b) for b in (bc_alive[:n_alive_target] + bc_deceased[:n_deceased_target])]
        for idx, bundle in enumerate(selected, start=1):
            _enrich_patient_bundle(bundle, idx)
        all_entries = []
        for bundle in selected:
            all_entries.extend(bundle.get('entry', []))
        all_entries.extend(hospital_entries)
        all_entries.extend(practitioner_entries)

        combined = {
            'resourceType': 'Bundle',
            'type': 'collection',
            'entry': all_entries,
        }

        os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
        with open(output, 'w') as f:
            json.dump(combined, f)

        # ------------------------------------------------------------------ #
        # 5. Summary
        # ------------------------------------------------------------------ #
        rt_counts = Counter(
            e['resource']['resourceType'] for e in all_entries
        )
        size_mb = os.path.getsize(output) / 1024 / 1024

        n_alive_actual = sum(1 for b in selected if not _is_deceased(b))
        n_deceased_actual = len(selected) - n_alive_actual
        self.stdout.write(self.style.SUCCESS(
            f'\nDone. {len(selected)} breast-cancer patients written to {output} '
            f'({n_alive_actual} alive, {n_deceased_actual} deceased; {size_mb:.1f} MB).'
        ))
        self.stdout.write('Resource type breakdown:')
        for rt, c in sorted(rt_counts.items(), key=lambda x: -x[1]):
            self.stdout.write(f'  {rt}: {c}')

        # Cleanup
        shutil.rmtree(output_dir, ignore_errors=True)
        if _cleanup_modules_dir:
            shutil.rmtree(modules_dir, ignore_errors=True)
