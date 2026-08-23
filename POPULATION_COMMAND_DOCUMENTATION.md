# PatientInfo Population Management Command

## Overview

This document describes the comprehensive `populate_patient_record` management command that was created to populate PatientInfo records from OMOP CDM and extension models. This command enables the transformation of normalized clinical data into comprehensive patient profiles suitable for clinical trial matching.

## What Was Built

### 1. Enhanced OMOP Data Model

The project was extended with comprehensive tables to support all PatientInfo fields:

#### Core OMOP Enhancements (omop_core/models.py)
- **Location**: Geographic location tracking with address, city, state, country
- **Person**: Enhanced with language support and death tracking for age calculation
- **ConditionOccurrence**: Standard OMOP condition tracking with cancer diagnoses
- **Measurement**: Laboratory and vital signs data using standard OMOP approach with LOINC concepts
- **ObservationExtension**: Performance status assessment details

#### Genomics Extensions (omop_genomics/models.py)
- **BiomarkerMeasurement**: Hormone receptors, HER2, PD-L1 testing
- **TumorAssessment**: RECIST evaluations and response tracking

#### Oncology Extensions (omop_oncology/models.py)
- **TreatmentLine**: Treatment line tracking with regimens and outcomes
- **SocialDeterminant**: Employment, insurance, housing status
- **HealthBehavior**: Smoking, alcohol, substance use history
- **InfectionStatus**: HIV, Hepatitis B/C, other infections

### 2. Population Management Command

#### Command Features
```bash
python manage.py populate_patient_record [options]

Options:
  --person-id ID     Process specific person only
  --force-update     Update existing PatientInfo records
  --verbose          Show detailed processing information
```

#### Data Extraction Functions

The command includes specialized functions to extract data from each model:

1. **get_demographics()**: Age calculation, gender mapping, race/ethnicity, language skills (from `PersonLanguageSkill` related objects, formatted as `"English: native, French: intermediate"`)
2. **get_location_data()**: Geographic information from Location model
3. **get_disease_data()**: Cancer diagnosis, staging, histology from ConditionOccurrence
4. **get_treatment_data()**: Treatment lines, regimens, responses from TreatmentLine; also populates `later_therapies` as a JSON array of `{therapy, startDate, endDate}` objects for 3rd line and beyond
5. **get_vitals_data()**: Blood pressure, weight, height from standard OMOP Measurement table
6. **get_biomarker_data()**: PD-L1, hormone receptors from BiomarkerMeasurement
7. **get_social_data()**: Employment, insurance from SocialDeterminant
8. **get_behavior_data()**: Smoking status from HealthBehavior
9. **get_infection_data()**: HIV, Hepatitis status from InfectionStatus
10. **get_assessment_data()**: Tumor response from TumorAssessment
11. **get_laboratory_data()**: Lab values from Measurement
12. **get_performance_data()**: ECOG, Karnofsky from Observation
13. **get_cll_data()**: CLL-specific fields extracted from OMOP clinical tables (see below)
14. **get_lymphoma_data()**: Follicular Lymphoma-specific fields (FLIPI score, GELF criteria, tumor grade)
15. **_compute_derived_fields()**: Post-extraction computation of fields that depend on other PatientInfo fields
16. **_compute_lymphocyte_doubling_time()**: Static helper — log-linear estimate from serial ALC measurements

##### get_cll_data() — CLL Field Extraction

Extracts the 22 CLL-specific fields from standard OMOP tables:

| Field | Source | LOINC / Concept |
|---|---|---|
| `absolute_lymphocyte_count` | Measurement | LOINC 26474-7 |
| `serum_beta2_microglobulin_level` | Measurement | LOINC 48436-3 |
| `qtcf_value` | Measurement | LOINC 8625-6 |
| `spleen_size` | Measurement | LOINC 59157-8 |
| `tumor_size` | Measurement | LOINC 21889-1 (`Size Tumor`) |
| `largest_lymph_node_size` | Measurement | LOINC 21889-1 only with `qualifier_source_value=lymph-node` legacy context |
| `clonal_bone_marrow_b_lymphocytes` | Measurement | LOINC 5905-5 |
| `binet_stage` | Observation | string value (A/B/C) |
| `tumor_burden` | Observation | string value |
| `disease_activity` | Observation | string value |
| `protein_expressions` | Observation | string value |
| `measurable_disease_iwcll` | Observation | boolean (1/0 or Yes/No string) |
| `hepatomegaly` | Observation | boolean |
| `splenomegaly` | Observation | boolean |
| `lymphadenopathy` | Observation | boolean |
| `autoimmune_cytopenias_refractory_to_steroids` | Observation | boolean |
| `bone_marrow_involvement` | Observation | boolean |
| `richter_transformation` | ConditionOccurrence | SNOMED concept (ICD10: C83.39) |
| `clonal_b_lymphocyte_count` | Observation | integer count |
| `btk_inhibitor_refractory` | DrugExposure + Observation | BTK drug followed by progression observation |
| `bcl2_inhibitor_refractory` | DrugExposure + Observation | BCL-2 drug followed by progression observation |
| `lymphocyte_doubling_time` | Measurement (serial ALC) | Computed via `_compute_lymphocyte_doubling_time()` |
| `tp53_disruption` | Computed | Set by `_compute_derived_fields()` from `genetic_mutations` JSON |

##### get_lymphoma_data() — Follicular Lymphoma Fields

| Field | Source | Notes |
|---|---|---|
| `flipi_score` | Observation | Integer (0–5) |
| `flipi_score_options` | Observation | String label (e.g. "Low", "Intermediate", "High") |
| `gelf_criteria_status` | Observation | String value |
| `tumor_grade` | Observation | Integer (1–3) |

##### _compute_derived_fields() — Computed Fields

Called after all other extraction methods on the unsaved `PatientInfo` instance:

- **`measurable_disease_imwg`**: `True` if serum M-protein ≥ 1 g/dL OR urine M-protein ≥ 200 mg/24h OR serum FLC ratio abnormal with involved FLC ≥ 100 mg/L. `False` if all three are measured but below thresholds. `None` if insufficient data.
- **`tp53_disruption`**: `True` if `genetic_mutations` JSON contains a mutation on gene `TP53` with classification `pathogenic`. `False` if TP53 entry exists but not pathogenic. `None` if no TP53 entry.

##### _compute_lymphocyte_doubling_time() — ALC Doubling Time

```
LDT (months) = months_elapsed × ln(2) / ln(ALC_last / ALC_first)
```

- Requires ≥ 2 ALC measurements taken at different times
- Returns `None` if ALC is not rising (ratio ≤ 1) or if only one data point exists
- Minimum returned value: 1 month (negative results indicate rapid progression but are clamped)

### 3. Comprehensive Field Mapping

The command maps data from OMOP models to PatientInfo fields:

#### Demographics
- Age calculated from birth year and death date
- Gender mapped from concept names
- Race/ethnicity from Person model
- Language preferences and skill levels (via `PersonLanguageSkill` junction model)

#### Disease Information
- Primary cancer diagnosis
- TNM staging (clinical and pathologic)
- Overall stage groups
- Histology and grade
- Primary site and metastatic status

#### Treatment History
- Treatment line numbers and regimens
- Treatment intent and setting
- Response outcomes
- Platinum, immunotherapy, chemotherapy flags
- `later_therapies`: JSON array of 3rd-line-and-beyond regimens with start/end dates

#### CLL-Specific
- All 22 CLL fields: staging (Binet), lab measurements (ALC, β2M, QTc), clinical observations (hepatomegaly, splenomegaly, lymphadenopathy), refractoriness (BTK/BCL-2 inhibitors), and computed values (lymphocyte doubling time, TP53 disruption)

#### Follicular Lymphoma-Specific
- FLIPI score (numeric) and risk category (string)
- GELF criteria status
- Tumor grade

#### Biomarkers
- PD-L1 expression and assay method
- Hormone receptor status (ER/PR)
- HER2 status
- Triple negative breast cancer determination

#### Clinical Measurements
- Vital signs (blood pressure, heart rate)
- Anthropometric data (weight, height, BMI)
- Laboratory values (hemoglobin, platelets, creatinine, etc.)
- Performance status (ECOG, Karnofsky)

#### Social and Behavioral
- Employment and insurance status
- Smoking and substance use history
- Infection status (HIV, Hepatitis)
- Geographic risk factors

## Usage Examples

### Basic Usage
```bash
# Populate all patients
python manage.py populate_patient_record

# Process specific patient with verbose output
python manage.py populate_patient_record --person-id 1001 --verbose

# Force update existing records
python manage.py populate_patient_record --force-update
```

### Integration with Other Commands
```bash
# Generate a FHIR bundle and import it, then query results
python manage.py generate_fhir_bundle --disease mm --count 50 --output /tmp/mm.json
python manage.py import_fhir_bundle /tmp/mm.json --org demo-org

# Query results
python manage.py query_patient_record
```

## Example Output

The command produces comprehensive PatientInfo records like:

```
Example populated record (Person 1001):
  Age: 63
  Gender: F
  Disease: Lung Cancer
  Stage: II
  Location: Illinois, United States
  Language: en
  Weight: 52.0 kg
  Height: 173.0 cm
  BMI: 17.37
  ECOG: 0
  Karnofsky: 80
  First Line Therapy: VRd
  First Line Date: 2025-08-09
  First Line Outcome: Partial Response
  Biomarkers:
    ER Status: Unknown
    PR Status: Unknown
```

## Clinical Trial Matching Benefits

The populated PatientInfo records contain comprehensive data for:

✓ **Demographics**: Age, gender, race, ethnicity, language preferences
✓ **Geographic**: Location data for site-specific trials
✓ **Disease**: Detailed cancer staging and histology
✓ **Treatment**: Complete therapy history and responses
✓ **Biomarkers**: Molecular markers for targeted therapy eligibility
✓ **Performance**: Functional status assessments
✓ **Laboratory**: Organ function and safety parameters
✓ **Social**: Insurance and support system factors
✓ **Behavioral**: Risk factor assessment
✓ **Medical**: Comorbidities and infection status

## Technical Architecture

The command follows Django best practices:
- Atomic transactions for data integrity
- Proper error handling and logging
- Modular design with specialized extraction functions
- Flexible options for different use cases
- Timezone-aware datetime handling

## Database Migrations

All new models were properly migrated:
```bash
python manage.py makemigrations
python manage.py migrate
```

The command successfully applied:
- omop_core.0003: Location, Person enhancements, vital signs, measurement extensions
- omop_genomics.0002: Biomarker measurements and tumor assessments
- omop_oncology.0002: Treatment lines, social determinants, health behaviors, infection status

## Future Enhancements

The foundation is now in place for:
1. Real-time PatientInfo updates when source data changes
2. Automated trial matching based on populated criteria
3. Export to clinical trial matching systems
4. Integration with EHR systems for data ingestion
5. Advanced analytics on patient populations

## Conclusion

The `populate_patient_record` management command successfully bridges the gap between normalized OMOP CDM data and the comprehensive PatientInfo model needed for clinical trial matching. It demonstrates how Django management commands can elegantly handle complex data transformations while maintaining data integrity and providing flexible operation modes.
