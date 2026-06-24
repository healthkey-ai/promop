#  PROMOP

**Measurement Table** (Standard OMOP CDM)
- **Vital Signs & Anthropometrics**: Essential measurements for clinical trial eligibility
  - Systolic BP (LOINC: 8480-6), Diastolic BP (LOINC: 8462-4)
  - Weight (LOINC: 29463-7), Height (LOINC: 8302-2), BMI (LOINC: 39156-5)
- **Laboratory Results**: Critical lab values for trial screening
  - Hemoglobin, Creatinine, Calcium, Albumin, Bilirubin, AST, ALT
- **Cancer Biomarkers**: Key biomarkers for trial matching
  - ER/PR status, HER2 status, PD-L1 expression
  - Genetic mutations (BRCA1/2, TP53, PIK3CA, KRAS, EGFR, ALK)

**Observation Table** (Standard OMOP CDM)rehensive OMOP CDM models enhanced with standard oncology extensions for cancer clinical trial matching.

## OMOP CDM Compliance & Clinical Trial Data Storage

This project follows **OMOP CDM v6.0 best practices** by storing all clinical data in standard OMOP tables using standardized vocabularies (LOINC, SNOMED CT, ICD-O-3). All extension models have been removed to ensure full OMOP compliance while maintaining comprehensive clinical trial eligibility screening capabilities.

### Standard OMOP Tables Used

#### Core OMOP CDM Tables (omop_core/models.py)

**Person Model** (Standard OMOP CDM)
- Standard OMOP Person table for patient demographics
- Connected to `PersonLanguageSkill` model for multiple language support

**🆕 PersonLanguageSkill Model** (Custom Addition for Clinical Trials)
- `language_concept` - Reference to language concept (English, Spanish, etc.)
- `skill_level` - Skill level choices: "speak", "write", "both"
- `is_primary` - Boolean flag for primary language
- Unique constraint per person/language combination
- **Purpose**: Clinical trials often require specific language capabilities for informed consent and communication

**Measurement Table** (Standard OMOP CDM)
- **Vital Signs & Anthropometrics**: Essential measurements for clinical trial eligibility
  - Systolic BP (LOINC: 8480-6), Diastolic BP (LOINC: 8462-4)
  - Weight (LOINC: 29463-7), Height (LOINC: 8302-2), BMI (LOINC: 39156-5)
- **Laboratory Results**: Critical lab values for trial screening
  - Hemoglobin, Creatinine, Calcium, Albumin, Bilirubin, AST, ALT
- **Cancer Biomarkers**: Key biomarkers for trial matching
  - ER/PR status, HER2 status, PD-L1 expression
  - Genetic mutations (BRCA1/2, TP53, PIK3CA, KRAS, EGFR, ALK)

**Observation Table** (Standard OMOP CDM)
- **Social Determinants**: Employment, insurance status for trial eligibility
- **Health Behaviors**: Tobacco use status critical for lung cancer trials
  - Never smoker (SNOMED: 266919005)
  - Former smoker (SNOMED: 8517006)
  - Current smoker (SNOMED: 77176002)
- **Infection Status**: HIV, Hepatitis testing results for trial exclusions
- **Cancer Staging & Treatment Response**: Disease progression assessments
  - Treatment response (Complete: 182840001, Partial: 182841002)
  - Stable disease (182843004), Progressive disease (182842009)

**DrugExposure Table** (Standard OMOP CDM)
- **Treatment History**: All cancer treatments and medications
- **Treatment Lines**: Derived from drug exposure patterns and dates
- **Therapy Classification**: Platinum-based, immunotherapy identification

**ConditionOccurrence Table** (Standard OMOP CDM)
- **Primary Diagnoses**: Cancer diagnoses with ICD-O-3 concepts
- **Comorbidities**: All medical conditions

#### Standard OMOP Oncology Extensions (omop_oncology/models.py)

**Episode Model** (Standard OMOP CDM v6.0)
- Disease episodes and treatment periods for cancer patients
- Links clinical events across multiple encounters

**EpisodeEvent Model** (Standard OMOP CDM v6.0)
- Links clinical events to disease episodes

**CancerModifier Model** (Standard OMOP Oncology Extension)
- Cancer-specific modifiers and qualifiers

**Histology Model** (Standard OMOP Oncology Extension)
- Cancer histology information using ICD-O-3 concepts

**StemTable Model** (Standard OMOP Oncology Extension)
- Pre-processing staging table for oncology data

## 🆕 Custom Extensions for Clinical Trial Matching

### Extensions Beyond Standard OMOP CDM & Official Extensions

While this project achieves full OMOP CDM v6.0 compliance, we have added **one key extension** to support comprehensive clinical trial patient matching:

#### 1. PersonLanguageSkill Model (Custom Addition)
**Purpose**: Clinical trials require specific language capabilities for informed consent and communication.

```python
class PersonLanguageSkill(models.Model):
    person = models.ForeignKey(Person, on_delete=models.CASCADE)
    language_concept = models.ForeignKey(Concept, on_delete=models.PROTECT)
    skill_level = models.CharField(choices=['speak/understand', 'read/write', 'both'])
    is_primary = models.BooleanField(default=False)
    
    class Meta:
        unique_together = ['person', 'language_concept']
```

**Key Features:**
- **Multi-language Support**: One person can have multiple language skills
- **Skill Level Granularity**: Distinguish between speaking, writing, or both capabilities
- **Primary Language**: Identify the patient's primary communication language
- **OMOP Integration**: Uses standard OMOP Concept table for language references
- **Clinical Trial Relevance**: Essential for patient consent and communication protocols

#### 2. PatientInfo Model (Integration/Denormalization Model)
**Purpose**: Research-friendly, denormalized view for rapid clinical trial eligibility screening.

- **Not an OMOP Extension**: This is an integration model that aggregates data from standard OMOP tables
- **Data Sources**: All data extracted from standard OMOP CDM tables (Person, Measurement, Observation, DrugExposure, ConditionOccurrence)
- **No Additional Storage**: Does not store additional clinical data beyond what's in OMOP tables
- **Performance Optimization**: Provides single-table access for common clinical trial queries

### All Other Data from Standard OMOP Tables

**✅ No Custom Clinical Data Storage:**
- All biomarkers → Standard Measurement table with LOINC concepts
- All lab values → Standard Measurement table with LOINC concepts  
- All vital signs → Standard Measurement table with LOINC concepts
- All social factors → Standard Observation table with SNOMED concepts
- All treatments → Standard DrugExposure table
- All conditions → Standard ConditionOccurrence table
- All staging data → Standard Observation table with cancer concepts

**✅ Standard OMOP Oncology Extensions Used:**
- Episode, EpisodeEvent, CancerModifier, Histology, StemTable models
- All part of official OMOP Oncology Extension specification

**✅ Removed Non-Standard Extensions:**
- ❌ BiomarkerMeasurement, TumorAssessment, TreatmentLine, SocialDeterminant, HealthBehavior, InfectionStatus
- All data migrated to standard OMOP tables for full compliance

### PatientInfo Model (Comprehensive Clinical Profile)
**Integration model with 100+ fields covering:**
- Demographics and anthropometrics
- Disease staging and histology
- Treatment history across all lines
- Biomarker results (ER, PR, HER2, PD-L1, genetic mutations)
- Laboratory values with units
- Performance status assessments
- Social determinants and risk factors
- Geographic and language information

## Management Commands

### populate_patient_info
**OMOP-compliant command** to extract data from standard OMOP tables and populate PatientInfo records:
```bash
python manage.py populate_patient_info --force-update --verbose
```

**Data Sources (All Standard OMOP Tables):**
- **Demographics**: Person table
- **Biomarkers**: Measurement table with LOINC concepts (ER, PR, HER2, PD-L1)
- **Lab Values**: Measurement table with LOINC concepts
- **Treatment History**: DrugExposure table (identifies platinum-based, immunotherapy)
- **Social Factors**: Observation table with SNOMED concepts
- **Health Behaviors**: Observation table (tobacco use, etc.)
- **Infection Status**: Measurement table (HIV, Hepatitis tests)
- **Disease Staging**: Observation table with cancer staging concepts

### manage_language_skills
Command to manage multiple language skills for persons:
```bash
# Create sample language concepts
python manage.py manage_language_skills --create-sample-concepts

# Add language skills
python manage.py manage_language_skills --person-id 1001 --add-language "English:both"
python manage.py manage_language_skills --person-id 1001 --add-language "Spanish:speak"

# Set primary language
python manage.py manage_language_skills --person-id 1001 --set-primary "English"

# List all languages for a person
python manage.py manage_language_skills --person-id 1001 --list-languages
```

### create_cancer_staging_observations
OMOP-compliant command to create cancer staging as Observation records:
```bash
# Create cancer staging concepts
python manage.py create_cancer_staging_observations --create-concepts

# Create staging observations for a condition
python manage.py create_cancer_staging_observations --person-id 1001 --condition-occurrence-id 5001
```

### migrate_vitals_to_measurement
Command to ensure vital signs are stored in standard OMOP Measurement table:
```bash
# Check what concepts would be created
python manage.py migrate_vitals_to_measurement --dry-run --verbose

# Create vital sign concepts for OMOP compliance
python manage.py migrate_vitals_to_measurement --verbose
```

## OMOP CDM v6.0 Compliance

This project achieves **full OMOP CDM v6.0 compliance** by storing all clinical data in standard OMOP tables using standardized vocabularies. No custom extension models are used, ensuring complete interoperability with OMOP analytical tools and other OMOP implementations.

### Clinical Data Storage Strategy

#### Biomarker Data → Measurement Table
**All biomarkers stored using standardized LOINC concepts:**

- **Estrogen Receptor (ER)**: LOINC 16112-5
- **Progesterone Receptor (PR)**: LOINC 16113-3  
- **HER2 Status**: LOINC 48676-1
- **PD-L1 Expression**: Custom LOINC concepts
- **Genetic Mutations**: LOINC concepts for specific mutations

#### Vital Signs & Anthropometrics → Measurement Table
**Essential measurements for clinical trial eligibility screening:**

- **Systolic Blood Pressure**: LOINC 8480-6 (concept_id: 3004249)
- **Diastolic Blood Pressure**: LOINC 8462-4 (concept_id: 3012888)  
- **Body Weight**: LOINC 29463-7 (concept_id: 3025315)
- **Body Height**: LOINC 8302-2 (concept_id: 3036277)
- **Body Mass Index**: LOINC 39156-5 (concept_id: 3038553)

#### Laboratory Values → Measurement Table
**Critical lab values for trial eligibility thresholds:**
- **Hemoglobin**: LOINC 718-7
- **Creatinine**: LOINC 2160-0
- **Calcium**: LOINC 17861-6
- **Albumin**: LOINC 1751-7
- **Bilirubin Total**: LOINC 1975-2
- **AST**: LOINC 1920-8
- **ALT**: LOINC 1742-6
- **Alkaline Phosphatase**: LOINC 6768-6

#### Social & Behavioral Data → Observation Table
**SNOMED CT concepts for clinical trial eligibility factors:**

- **Employment Status**: SNOMED 224362002 (trial accessibility)
- **Insurance Status**: SNOMED 408729009 (trial coverage)
- **Tobacco Use Status** (critical for lung cancer trials):
  - Never smoked: SNOMED 266919005
  - Former smoker: SNOMED 8517006
  - Current smoker: SNOMED 77176002

#### Infection Status → Measurement Table
**LOINC concepts for infectious disease testing (trial exclusion criteria):**

- **HIV Tests**: LOINC 5221-7 (HIV 1 Ab), LOINC 7917-8 (HIV 1+2 Ab)
- **Hepatitis B**: LOINC 5195-3 (HBsAg)
- **Hepatitis C**: LOINC 5196-1 (HCV Ab)

#### Treatment Response → Observation Table
**SNOMED CT concepts for tumor assessment (response evaluation criteria):**

- **Complete Response**: SNOMED 182840001
- **Partial Response**: SNOMED 182841002
- **Stable Disease**: SNOMED 182843004
- **Progressive Disease**: SNOMED 182842009

#### Treatment History → DrugExposure Table
**Standard OMOP drug exposure tracking for treatment line analysis:**
- Treatment line identification from drug exposure patterns
- Platinum-based therapy identification via drug concepts
- Immunotherapy classification via drug concepts
- Concomitant medication tracking for drug interactions

### Cancer Staging → Observation Table
**OMOP CDM best practice for cancer staging:**

- **Maintains OMOP Standards**: Uses existing OMOP tables as designed
- **Supports Standard Vocabularies**: Leverages ICD-O-3, SNOMED-CT terminologies
- **Enables Flexible Staging**: Supports any staging system (AJCC, TNM, etc.)
- **Links Related Data**: Uses CDM v6.0 `observation_event_id` to link staging to conditions

**Cancer Data Storage Pattern:**
- **Primary Site**: Observation with ICD-O-3 topography concept
- **Histology**: Observation with ICD-O-3 morphology concept  
- **TNM Staging**: Separate observations for T, N, M categories
- **Stage Group**: Observation with overall stage concept
- **Grade**: Observation with tumor grade concept

## Recent Updates

### Full OMOP CDM v6.0 Compliance Achieved
The project has been fully refactored to achieve **complete OMOP CDM compliance** by removing all non-standard extension models and storing all clinical data in standard OMOP tables:

**Key Changes:**
- ✅ Removed all custom extension models (BiomarkerMeasurement, TreatmentLine, etc.)
- ✅ Migrated all data to standard OMOP tables (Measurement, Observation, DrugExposure)
- ✅ Implemented standardized vocabulary usage (LOINC, SNOMED CT, ICD-O-3)
- ✅ Updated data extraction logic to use standard OMOP patterns
- ✅ Maintained all clinical trial eligibility screening capabilities

### PatientInfo Model Integration
A comprehensive PatientInfo model provides a research-friendly, denormalized view of patient data optimized for rapid realtime clinical trial eligibility screening while sourcing all data from standard OMOP tables.

**Data Sources (All OMOP-Compliant):**
- **Demographics**: Person table
- **Biomarkers**: Measurement table with LOINC concepts
- **Treatment History**: DrugExposure table analysis
- **Laboratory Values**: Measurement table with proper units
- **Social Factors**: Observation table with SNOMED concepts
- **Cancer Staging**: Observation table with standardized concepts
- **Vital Signs**: Measurement table with LOINC concepts

**Key Features:**
- Complete patient demographics and disease information
- Treatment history tracking (1st, 2nd, later lines) from DrugExposure patterns
- Laboratory values with proper units from Measurement table
- Cancer-specific biomarkers (ER, PR, HER2, PD-L1) from Measurement table
- Risk factors and behavioral data from Observation table
- Automatic BMI calculation from height/weight measurements
- Full integration with standard OMOP CDM tables

**Documentation:** 
- See [PATIENTINFO_README.md](PATIENTINFO_README.md) for detailed usage information
- See [OMOP2PatientInfo.md](OMOP2PatientInfo.md) for complete OMOP CDM to PatientInfo mapping documentation

## Quick Start with PatientInfo

```bash
# Create sample data
python manage.py populate_patient_info --count 5

# Query patient information
python manage.py query_patient_info

# Query specific patient
python manage.py query_patient_info --person-id 1001

# Filter by disease
python manage.py query_patient_info --disease "breast"
```

## Project Structure

The project consists of three main Django apps designed for **full OMOP CDM v6.0 compliance**:

- **omop_core**: Standard OMOP CDM core tables plus PatientInfo integration model
  - Person, Measurement, Observation, DrugExposure, ConditionOccurrence
  - PersonLanguageSkill for multi-language support
  - PatientInfo model for clinical trial eligibility screening

- **omop_genomics**: Placeholder for future genomic data (currently uses standard OMOP tables)
  - All genomic data stored in Measurement and Observation tables
  - Uses standardized LOINC concepts for genetic tests

- **omop_oncology**: Standard OMOP oncology extensions only
  - Episode, EpisodeEvent, CancerModifier, Histology models
  - All treatment and assessment data in standard OMOP tables

**Data Architecture:** All clinical trial matching data is extracted from standard OMOP tables using standardized vocabularies, ensuring complete interoperability with OMOP analytical tools and other OMOP CDM implementations.

## Summary of Extensions Beyond Standard OMOP CDM

### ✅ **Single Custom Extension Added:**
1. **PersonLanguageSkill Model** - Multi-language support for clinical trial communication requirements

### ✅ **Standard OMOP Extensions Used:**
- **OMOP Oncology Extension**: Episode, EpisodeEvent, CancerModifier, Histology, StemTable
- **OMOP Genomics Extension**: Placeholder (currently using standard Measurement/Observation tables)

### ✅ **Integration Models (Not Extensions):**
- **PatientInfo Model** - Denormalized view aggregating data from standard OMOP tables for performance

### ✅ **Full OMOP Compliance Achieved:**
- All clinical data stored in standard OMOP CDM v6.0 tables
- Standardized vocabularies used (LOINC, SNOMED CT, ICD-O-3, RxNorm)
- Complete interoperability with OMOP analytical tools
- No non-standard clinical data storage

This ensures **full OMOP CDM compliance** while maintaining all clinical trial eligibility screening capabilities through intelligent extraction from standard OMOP tables.
