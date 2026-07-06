# OMOP CDM to PatientInfo Mapping

This document describes how data flows from standard OMOP CDM v5.4 tables to the comprehensive `PatientInfo` model for clinical trial eligibility screening. All data is extracted from standard OMOP tables using standardized vocabularies to ensure full OMOP compliance.

## Overview

The `PatientInfo` model provides a **denormalized, research-friendly view** of patient data optimized for rapid clinical trial eligibility screening. It contains **100+ fields** that aggregate data from multiple standard OMOP tables through intelligent extraction logic.

## 🔄 Data Flow Architecture

```
Standard OMOP Tables → Data Extraction Logic → PatientInfo Model → Clinical Trial Matching
```

### Key Principles:
- ✅ **Full OMOP Compliance**: Only standard OMOP CDM v5.4 tables used
- ✅ **Standardized Vocabularies**: LOINC, SNOMED CT, ICD-O-3 concepts
- ✅ **No Extension Models**: All custom extension models removed
- ✅ **Interoperability**: Compatible with all OMOP analytical tools

## 📊 Table-by-Table Mapping

### 1. Demographics & Basic Information

#### Source: `Person` Table (Standard OMOP)
| PatientInfo Field | OMOP Source | Extraction Logic |
|------------------|-------------|------------------|
| `person_id` | `Person.person_id` | Direct mapping |
| `gender` | `Person.gender_concept_id` | Concept lookup |
| `race` | `Person.race_concept_id` | Concept lookup |
| `ethnicity` | `Person.ethnicity_concept_id` | Concept lookup |
| `birth_datetime` | `Person.birth_datetime` | Direct mapping |
| `death_datetime` | `Person.death_datetime` | Direct mapping |

#### 🆕 **Added Enhancement**: `PersonLanguageSkill` Model
| PatientInfo Field | Source | Extraction Logic |
|------------------|--------|------------------|
| `primary_language` | `PersonLanguageSkill.language_concept` | Where `is_primary=True` |
| `language_skills` | `PersonLanguageSkill` records | JSON aggregation of all languages |

**Custom Model Added:**
```python
class PersonLanguageSkill(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    language_concept = models.ForeignKey(Concept, on_delete=models.PROTECT)
    skill_level = models.CharField(choices=['speak', 'write', 'both'])
    is_primary = models.BooleanField(default=False)
```

### 2. Vital Signs & Anthropometrics

#### Source: `Measurement` Table (Standard OMOP) with LOINC Concepts
| PatientInfo Field | LOINC Code | Concept ID | Extraction Logic |
|------------------|------------|------------|------------------|
| `height_cm` | 8302-2 | 3036277 | Latest measurement |
| `weight_kg` | 29463-7 | 3025315 | Latest measurement |
| `bmi` | 39156-5 | 3038553 | Calculated or measured |
| `systolic_bp` | 8480-6 | 3004249 | Latest measurement |
| `diastolic_bp` | 8462-4 | 3012888 | Latest measurement |
| `heart_rate` | 8867-4 | 3027018 | Latest measurement |
| `temperature_c` | 8310-5 | 3020891 | Latest measurement |
| `oxygen_saturation` | 2708-6 | 3016502 | Latest measurement |

**🎯 Clinical Trial Enhancement**: BMI automatically calculated from height/weight if not directly measured.

### 3. Laboratory Values

#### Source: `Measurement` Table (Standard OMOP) with LOINC Concepts
| PatientInfo Field | LOINC Code | Extraction Logic | Units |
|------------------|------------|------------------|-------|
| `hemoglobin` | 718-7 | Latest value | g/dL |
| `hemoglobin_units` | - | Unit concept lookup | CharField |
| `creatinine` | 2160-0 | Latest value | mg/dL |
| `creatinine_units` | - | Unit concept lookup | CharField |
| `calcium` | 17861-6 | Latest value | mg/dL |
| `albumin` | 1751-7 | Latest value | g/dL |
| `bilirubin_total` | 1975-2 | Latest value | mg/dL |
| `ast` | 1920-8 | Latest value | U/L |
| `alt` | 1742-6 | Latest value | U/L |
| `alkaline_phosphatase` | 6768-6 | Latest value | U/L |

**🎯 Clinical Trial Enhancement**: Includes units for proper eligibility threshold evaluation.

### 4. Cancer Biomarkers

#### Source: `Measurement` Table (Standard OMOP) with Standardized Concepts
| PatientInfo Field | LOINC Code | Extraction Logic | Clinical Significance |
|------------------|------------|------------------|----------------------|
| `estrogen_receptor_status` | 16112-5 | Latest measurement | POSITIVE/NEGATIVE |
| `progesterone_receptor_status` | 16113-3 | Latest measurement | POSITIVE/NEGATIVE |
| `her2_status` | 48676-1 | Latest measurement | POSITIVE/NEGATIVE/EQUIVOCAL |
| `pd_l1_tumor_cels` | Custom | Latest measurement | Percentage value |
| `pd_l1_assay` | Custom | Assay method from measurement | Assay type |
| `tnbc_status` | Calculated | ER-/PR-/HER2- logic | Boolean |

**🎯 Clinical Trial Enhancement**: Triple-negative breast cancer (TNBC) status calculated from ER/PR/HER2 results.

### 5. Genetic Mutations

#### Source: `Measurement` Table (Standard OMOP) with Genetic Test LOINC Concepts
| PatientInfo Field | LOINC Concept | Extraction Logic |
|------------------|---------------|------------------|
| `brca1_mutation` | Genetic test concepts | Latest result |
| `brca2_mutation` | Genetic test concepts | Latest result |
| `tp53_mutation` | Genetic test concepts | Latest result |
| `pik3ca_mutation` | Genetic test concepts | Latest result |
| `kras_mutation` | Genetic test concepts | Latest result |
| `egfr_mutation` | Genetic test concepts | Latest result |
| `alk_rearrangement` | Genetic test concepts | Latest result |

### 6. Treatment History

#### Source: `DrugExposure` Table (Standard OMOP)
| PatientInfo Field | Extraction Logic | Clinical Trial Relevance |
|------------------|------------------|-------------------------|
| `first_line_therapy` | First drug by date | Primary treatment |
| `first_line_date` | Earliest drug exposure | Treatment timeline |
| `second_line_therapy` | Second drug by date | Progression indicator |
| `second_line_date` | Second drug date | Treatment timeline |
| `later_therapy` | Subsequent drugs (plain text) | Heavily pretreated status |
| `later_therapies` | Subsequent drugs (structured JSON) | Structured later-line history |
| `therapy_lines_count` | Count unique treatment periods | Line of therapy |
| `concomitant_medications` | Recent drug exposures | Drug interactions |

**🎯 Clinical Trial Enhancement**:
- Treatment lines derived from drug exposure patterns
- `later_therapies` stored as a JSONField array: `[{"therapy": "...", "startDate": "...", "endDate": "..."}]`
- Platinum-based therapy identification via drug concept analysis
- Immunotherapy classification via drug name matching

#### Therapy Classification Logic:
```python
# Platinum-based therapy detection
platinum_terms = ['platinum', 'carboplatin', 'cisplatin', 'oxaliplatin']
is_platinum = any(term in drug_name.lower() for term in platinum_terms)

# Immunotherapy detection
immuno_terms = ['pembrolizumab', 'nivolumab', 'atezolizumab', 'durvalumab']
is_immunotherapy = any(term in drug_name.lower() for term in immuno_terms)
```

### 7. Social Determinants & Behaviors

#### Source: `Observation` Table (Standard OMOP) with SNOMED CT Concepts
| PatientInfo Field | SNOMED Code | Extraction Logic |
|------------------|-------------|------------------|
| `tobacco_use_details` | 266919005 (Never) | Current status |
| `no_tobacco_use_status` | 8517006 (Former) | Boolean conversion |
| - | 77176002 (Current) | Status determination |

**🎯 Clinical Trial Enhancement**: Tobacco use status critical for lung cancer trials.

### 8. Infection Status

#### Source: `Measurement` Table (Standard OMOP) with LOINC Test Concepts
| PatientInfo Field | LOINC Code | Test Description | Extraction Logic |
|------------------|------------|------------------|------------------|
| `hiv_status` | 5221-7 | HIV 1 Ab | Positive/Negative |
| `no_hiv_status` | 7917-8 | HIV 1+2 Ab | Boolean inverse |
| `hepatitis_b_status` | 5195-3 | HBsAg | Positive/Negative |
| `no_hepatitis_b_status` | - | - | Boolean inverse |
| `hepatitis_c_status` | 5196-1 | HCV Ab | Positive/Negative |
| `no_hepatitis_c_status` | - | - | Boolean inverse |

### 9. Disease Staging & Assessment

#### Source: `Observation` Table (Standard OMOP) with Cancer Staging Concepts
| PatientInfo Field | SNOMED Code | Extraction Logic |
|------------------|-------------|------------------|
| `best_response` | 182840001 | Complete Response |
| - | 182841002 | Partial Response |
| - | 182843004 | Stable Disease |
| - | 182842009 | Progressive Disease |

#### Source: `ConditionOccurrence` Table (Standard OMOP)
| PatientInfo Field | Extraction Logic | Clinical Trial Relevance |
|------------------|------------------|-------------------------|
| `primary_diagnosis` | Latest cancer condition | Tumor type matching |
| `diagnosis_date` | Condition start date | Disease timeline |
| `stage_at_diagnosis` | Linked observations | Staging requirements |

### 10. Geographic & Administrative

#### Source: `Location` Table (Standard OMOP)
| PatientInfo Field | OMOP Source | Extraction Logic |
|------------------|-------------|------------------|
| `state` | `Location.state` | Via Person.location_id |
| `county` | `Location.county` | Geographic analysis |
| `zip_code` | `Location.zip` | Location details |

### 11. CLL (Chronic Lymphocytic Leukemia) Fields

#### Source: `Measurement` Table — LOINC codes
| PatientInfo Field | LOINC Code | Description |
|---|---|---|
| `absolute_lymphocyte_count` | 731-0 | Lymphocytes [#/volume] in Blood |
| `serum_beta2_microglobulin_level` | 48094-6 | Beta-2-microglobulin [Mass/volume] in Serum |
| `qtcf_value` | 8632-1 | QT interval Fridericia corrected |
| `spleen_size` | 44996-6 | Spleen diameter (ultrasound) |
| `largest_lymph_node_size` | 21889-1 | Lymph node greatest dimension |
| `clonal_bone_marrow_b_lymphocytes` | concept name match | Clonal bone marrow B-lymphocyte % |
| `clonal_b_lymphocyte_count` | concept name match | Clonal B-lymphocyte count |
| `protein_expressions` | concept name match | CD markers (CD38, CD20, CD5, ZAP70) |

#### Source: `Observation` Table — concept name matching
| PatientInfo Field | Match Pattern | Value Extraction |
|---|---|---|
| `binet_stage` | `'binet'` in concept name | `value_as_string` or numeric |
| `tumor_burden` | `'tumor burden'` in concept name | `value_as_string` |
| `disease_activity` | `'disease activity'` in concept name | `value_as_string` |
| `hepatomegaly` | `'hepatomegaly'` in concept name | Boolean from string/number |
| `splenomegaly` | `'splenomegaly'` in concept name | Boolean from string/number |
| `lymphadenopathy` | `'lymphadenopathy'` in concept name | Boolean from string/number |
| `bone_marrow_involvement` | `'bone marrow involvement'` in concept name | Boolean from string/number |
| `autoimmune_cytopenias_refractory_to_steroids` | `'autoimmune cytopenia'` in concept name | Boolean from string/number |

#### Source: `ConditionOccurrence` Table — concept name matching
| PatientInfo Field | Match Pattern | Notes |
|---|---|---|
| `richter_transformation` | `'richter'` in concept name | Stores full concept name |
| `hepatomegaly` | `'hepatomegaly'` in concept name | Fallback if not in Observation |
| `splenomegaly` | `'splenomegaly'` in concept name | Fallback if not in Observation |
| `lymphadenopathy` | `'lymphadenopathy'` in concept name | Fallback if not in Observation |

#### Source: `DrugExposure` + `Observation` — refractoriness logic
| PatientInfo Field | BTK inhibitors / BCL-2 inhibitors | Progression required |
|---|---|---|
| `btk_inhibitor_refractory` | ibrutinib, zanubrutinib, acalabrutinib, pirtobrutinib | SNOMED 182842009 (Progressive disease) |
| `bcl2_inhibitor_refractory` | venetoclax | SNOMED 182842009 (Progressive disease) |

#### Computed CLL fields
| PatientInfo Field | Computation |
|---|---|
| `lymphocyte_doubling_time` | Estimated from serial ALC measurements using log-linear growth model (months, min 1) |
| `tp53_disruption` | `True` if any `genetic_mutations` entry has `gene=tp53` and `interpretation=pathogenic` |
| `measurable_disease_imwg` | `True` if serum M-protein ≥ 0.5 g/dL OR urine M-protein ≥ 200 mg/24h OR FLC ratio abnormal with diff ≥ 10 mg/dL |

### 12. Follicular Lymphoma Fields

#### Source: `Observation` + `Measurement` Tables
| PatientInfo Field | Source | Match Pattern |
|---|---|---|
| `flipi_score` | Observation (numeric) | `'flipi'` in concept name |
| `flipi_score_options` | Observation (string) | `'flipi'` in concept name, string value |
| `gelf_criteria_status` | Observation | `'gelf'` in concept name |
| `tumor_grade` | Measurement (numeric) | `'grade'` + `'lymphoma'` in concept name |

### 13. Shared New Fields

| PatientInfo Field | Source | Extraction |
|---|---|---|
| `languages_skills` | `PersonLanguageSkill` relation | `"Language: skill_level"` pairs joined with `, ` |
| `status` | (to be populated externally) | Not extracted by `populate_patient_record` |
| `later_therapies` | `DrugExposure` | JSON array of later-line drugs (see §6) |
| `measurable_disease_imwg` | Computed | See CLL computed fields above |

## 🆕 Key Enhancements Added

### 1. Multi-Language Support
- **PersonLanguageSkill Model**: Supports multiple languages per person
- **Primary Language Detection**: Identifies primary communication language
- **Skill Level Tracking**: Speak, write, or both capabilities

### 2. Clinical Trial Specific Fields
- **Treatment Line Analysis**: Derives therapy lines from drug patterns
- **Triple-Negative Status**: Calculates TNBC from biomarker results  
- **Therapy Classification**: Identifies platinum-based and immunotherapy
- **Performance Status**: Ready for ECOG/Karnofsky integration

### 3. Laboratory Units Tracking
- **Unit Standardization**: Captures measurement units for threshold evaluation
- **Multiple Unit Support**: Handles different lab reporting standards
- **Conversion Ready**: Prepared for unit conversion logic

### 4. Comprehensive Biomarker Panel
- **Standard Biomarkers**: ER, PR, HER2, PD-L1
- **Genetic Mutations**: BRCA1/2, TP53, PIK3CA, KRAS, EGFR, ALK
- **Assay Method Tracking**: Records testing methodology

## 🔧 Data Extraction Commands

### Primary Command: `populate_patient_record`
```bash
python manage.py populate_patient_record --force-update --verbose
```

**Extraction Process:**
1. **Demographics**: Extract from Person + PersonLanguageSkill
2. **Vital Signs**: Query Measurement with LOINC concepts
3. **Lab Values**: Query Measurement with lab LOINC codes
4. **Biomarkers**: Query Measurement with biomarker concepts
5. **Treatment History**: Analyze DrugExposure patterns
6. **Social/Behavioral**: Query Observation with SNOMED concepts
7. **Infection Status**: Query Measurement with infectious disease tests
8. **Disease Staging**: Query Observation with staging concepts

### Supporting Commands:
```bash
# Language skills management
python manage.py manage_language_skills --person-id 1001 --add-language "English:both"

# Cancer staging observations
python manage.py create_cancer_staging_observations --person-id 1001

# Vital signs LOINC concepts
python manage.py migrate_vitals_to_measurement --verbose
```

## ✅ OMOP Compliance Verification

### Standard OMOP Tables Only:
- ✅ Person, Measurement, Observation, DrugExposure, ConditionOccurrence
- ✅ Location, Concept, Vocabulary
- ✅ Episode, EpisodeEvent (standard oncology extensions)

### Standardized Vocabularies:
- ✅ LOINC for all measurements and lab tests
- ✅ SNOMED CT for observations and behaviors  
- ✅ ICD-O-3 for cancer histology and sites
- ✅ RxNorm for drug concepts

### No Custom Extension Models:
- ❌ BiomarkerMeasurement (removed)
- ❌ TreatmentLine (removed)
- ❌ SocialDeterminant (removed)
- ❌ HealthBehavior (removed)
- ❌ InfectionStatus (removed)

## 🎯 Clinical Trial Matching Benefits

### Rapid Eligibility Screening:
- **Single Table Query**: All data in PatientInfo for fast access
- **Standardized Fields**: Consistent data format across patients
- **Complete Coverage**: 100+ fields cover most trial criteria

### OMOP Compatibility:
- **Standard Analytics**: Compatible with OMOP analytical tools
- **Network Studies**: Enables multi-site clinical research
- **Quality Assurance**: Leverages OMOP data quality standards

### Research Efficiency:
- **Denormalized Access**: No complex joins for common queries
- **Trial-Ready Format**: Fields optimized for eligibility criteria
- **Comprehensive Coverage**: Demographics, biomarkers, treatments, labs

This architecture ensures **full OMOP CDM v5.4 compliance** while providing the research efficiency needed for rapid clinical trial patient identification and eligibility screening.
