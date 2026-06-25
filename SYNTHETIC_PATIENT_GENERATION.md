# Synthetic Patient Generation — Status & Options

## Available Commands

| Command | Status | What it does |
|---|---|---|
| `seed_test_patients` | **Working** | Creates 7 hardcoded patients directly into `PatientInfo` (2 MM, 2 FL, 1 BC, 2 CLL, person_ids 9001–9007). No OMOP pipeline. |
| `populate_patient_info` | **Working** | Reads existing OMOP tables (Person, Measurement, ConditionOccurrence, DrugExposure, Observation) and derives `PatientInfo` records. Requires source OMOP data to exist first. |
| `create_enhanced_sample_data` | **Broken** | Was supposed to create OMOP source records and a `PatientInfo` record for a single lung cancer patient. |
| `generate_breast_cancer_patients` | Untested | Generates breast cancer patients specifically. |
| `generate_fhir_bundle` | Untested | Generates breast cancer patients specifically. |

## `create_enhanced_sample_data` — What's Broken

### 1. Missing Model Imports (6 models removed for OMOP compliance)

```python
from omop_genomics.models import BiomarkerMeasurement, TumorAssessment        # both removed
from omop_oncology.models import TreatmentLine, SocialDeterminant, HealthBehavior, InfectionStatus  # all removed
```

These models were deliberately removed from the codebase. The apps now enforce strict OMOP CDM compliance — all data should be stored in standard OMOP tables:

| Removed Model | Replacement Table | Notes |
|---|---|---|
| `BiomarkerMeasurement` | `Measurement` | PD-L1, ER/PR/HER2 as LOINC-coded measurements |
| `TumorAssessment` | `Observation` | RECIST assessments as observations |
| `TreatmentLine` | `Episode` | Treatment lines as episode records |
| `SocialDeterminant` | `Observation` | Employment, insurance as observations |
| `HealthBehavior` | `Observation` | Smoking, substance use as observations |
| `InfectionStatus` | `Observation` / `Measurement` | HIV, Hepatitis status as observations |

**Source**: Comments in `omop_genomics/models.py` (lines 4–9) and `omop_oncology/models.py` (lines 132–137) explicitly state this design decision.

### 2. Wrong PatientInfo Field Names

The command creates `PatientInfo` with field names from an older schema version:

| Used in command | Actual field name |
|---|---|
| `patient_info_id` | `id` (auto) |
| `age` | `patient_age` |
| `primary_diagnosis` | `disease` |
| `cancer_stage` | `stage` |
| `cancer_stage_system` | *(doesn't exist)* |
| `tnm_t` | `tumor_stage` |
| `tnm_n` | `nodes_stage` |
| `tnm_m` | `distant_metastasis_stage` |
| `pdl1_expression` | `pd_l1_tumor_cels` |
| `pdl1_assay` | `pd_l1_assay` |
| `weight_kg` | `weight` |
| `height_cm` | `height` |
| `smoking_status` | `no_tobacco_use_status` (boolean) |
| `hiv_status` | `no_hiv_status` (boolean) |
| `employment_status` | *(doesn't exist)* |
| `insurance_type` | *(doesn't exist)* |
| `best_response` | *(doesn't exist on PatientInfo)* |

### 3. Only 1 Patient

The command creates a single lung cancer patient (person_id=1001). Lung cancer isn't even a supported disease in the matching engine (only MM, FL, BC, CLL).

## How to Fix

To make `create_enhanced_sample_data` functional:

1. **Remove** all 6 deleted model imports and usages
2. **Replace** with standard OMOP table inserts:
   - `BiomarkerMeasurement` → `Measurement` with LOINC concept codes
   - `TumorAssessment` → `Observation` records
   - `TreatmentLine` → `Episode` records
   - `SocialDeterminant`, `HealthBehavior`, `InfectionStatus` → `Observation` records
3. **Remove** the direct `PatientInfo` creation (let `populate_patient_info` derive it)
4. **Add** patients for supported diseases (MM, FL, BC, CLL) instead of lung cancer
5. **Workflow** after fix:
   ```bash
   python manage.py create_enhanced_sample_data
   python manage.py populate_patient_info --force-update
   ```

## Recommended Workflow for Local Testing

Until `create_enhanced_sample_data` is fixed, use:

```bash
# From promop directory:
DATABASE_URL=postgresql://... python manage.py seed_test_patients

# Verify:
DATABASE_URL=postgresql://... python manage.py query_patient_info
```

This creates 7 patients across all 4 diseases with clinically realistic field values, ready for trial matching via `search_trials_for_promop_patients`.
