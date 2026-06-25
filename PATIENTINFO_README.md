# PatientInfo Model

This document describes the PatientInfo model that has been added to the CTOMOP project, adapted from the exapromop repository.

## Overview

The PatientInfo model provides a comprehensive, denormalized view of patient data optimized for clinical trial eligibility screening and research applications. It consolidates data from multiple OMOP CDM tables into a single, research-friendly record per patient.

## Model Features

### Demographics
- Age, gender, weight, height (with automatic BMI calculation)
- Geographic location (country, region, postal code, coordinates)
- Language information
- Ethnicity data

### Disease Information
- Primary disease/diagnosis
- Disease stage
- Performance status (Karnofsky, ECOG)
- Active malignancies status

### Treatment History
- Prior therapy information
- Treatment line data (1st, 2nd, later lines)
- Therapy dates and outcomes
- Treatment response tracking

### Laboratory Values
- Complete blood count (hemoglobin, platelets, WBC, RBC)
- Chemistry panel (creatinine, calcium, bilirubin, albumin)
- Liver function tests (AST, ALT, ALP)
- All values include units for proper interpretation

### Cancer-Specific Fields
- Hormone receptor status (ER, PR, HER2)
- TNM staging components
- Genetic mutations (JSON field)
- Biomarker results (PD-L1, Ki-67)
- Cancer subtype classifications

### Risk Factors & Behavioral Data
- Tobacco and substance use
- Infection status (HIV, Hepatitis B/C)
- Pregnancy/lactation status
- Mental health status
- Caregiver availability
- Geographic exposure risks

## Usage Examples

### Creating a PatientInfo Record

```python
from omop_core.models import Person, PatientInfo

# Create or get a Person record
person = Person.objects.get(person_id=1001)

# Create PatientInfo
patient_info = PatientInfo.objects.create(
    person=person,
    patient_age=65,
    gender='F',
    weight=70.5,
    weight_units='kg',
    height=165,
    height_units='cm',
    disease='Breast Cancer',
    stage='II',
    ecog_performance_status=1,
    hemoglobin_level=12.5,
    hemoglobin_level_units='G/DL',
    estrogen_receptor_status='Positive',
    progesterone_receptor_status='Positive',
    her2_status='Negative'
)

# BMI is automatically calculated
print(f"Patient BMI: {patient_info.bmi}")
```

### Querying PatientInfo

```python
# Find all breast cancer patients
breast_cancer_patients = PatientInfo.objects.filter(disease__icontains='breast')

# Find patients with multiple treatment lines
multi_line_patients = PatientInfo.objects.filter(therapy_lines_count__gt=1)

# Find patients eligible for certain criteria
eligible_patients = PatientInfo.objects.filter(
    ecog_performance_status__lte=1,
    hemoglobin_level__gte=10.0,
    no_active_infection_status=True
)

# Complex queries with genetic mutations
brca_patients = PatientInfo.objects.filter(
    genetic_mutations__contains=['BRCA1']
)
```

### Updating Laboratory Values

```python
patient_info = PatientInfo.objects.get(person__person_id=1001)
patient_info.hemoglobin_level = 11.2
patient_info.platelet_count = 150000
patient_info.serum_creatinine_level = 1.1
patient_info.save()
```

## Management Commands

### Populate Sample Data

```bash
# Create 5 sample patients with PatientInfo
python manage.py populate_patient_info

# Create 20 sample patients
python manage.py populate_patient_info --count 20

# Clean existing data and create new samples
python manage.py populate_patient_info --count 10 --clean
```

## Admin Interface

The PatientInfo model is registered in the Django admin with organized fieldsets:

- Patient Link
- Demographics
- Disease Information
- Treatment History
- Laboratory Values
- Cancer-Specific
- Risk Factors & Behavior
- Geographic Information

Access via: `/admin/omop_core/patientinfo/`

## Database Schema

The model uses the following database table structure:

- **Table name**: `patient_info`
- **Primary key**: Auto-generated `id`
- **Foreign key**: `person_id` (OneToOne relationship with Person)
- **Indexes**: person, patient_age, disease, stage

## Field Types and Choices

### Choice Fields
- `gender`: M (Male), F (Female), U (Unknown)
- `weight_units`: kg (Kilograms), lb (Pounds)
- `height_units`: cm (Centimeters), in (Inches)
- Various lab unit choices for proper unit tracking

### JSON Fields
- `genetic_mutations`: Array of genetic mutation names
- `stem_cell_transplant_history`: Array of transplant records

### Computed Fields
- `bmi`: Automatically calculated from weight and height

## Integration with OMOP CDM

The PatientInfo model is designed to complement, not replace, the standard OMOP CDM tables:

1. **Person**: OneToOne relationship maintains OMOP compliance
2. **Measurement**: Lab values can be cross-referenced with source measurements
3. **Observation**: Clinical observations can be linked to PatientInfo fields
4. **ConditionOccurrence**: Disease information connects to condition records
5. **DrugExposure**: Treatment history relates to drug exposure records

## Clinical Trial Matching

The model structure supports clinical trial eligibility screening:

```python
def check_trial_eligibility(patient_info, trial_criteria):
    eligible = True
    
    # Age criteria
    if trial_criteria.get('min_age') and patient_info.patient_age < trial_criteria['min_age']:
        eligible = False
    
    # Performance status
    if trial_criteria.get('max_ecog') and patient_info.ecog_performance_status > trial_criteria['max_ecog']:
        eligible = False
    
    # Lab values
    if trial_criteria.get('min_hemoglobin') and patient_info.hemoglobin_level < trial_criteria['min_hemoglobin']:
        eligible = False
    
    # Biomarkers
    if trial_criteria.get('required_mutations'):
        if not any(mut in patient_info.genetic_mutations for mut in trial_criteria['required_mutations']):
            eligible = False
    
    return eligible
```

## Migration

The PatientInfo model has been added via Django migration:
- **Migration file**: `omop_core/migrations/0002_patientinfo.py`
- **Run migration**: `python manage.py migrate omop_core`

## Future Enhancements

Potential areas for extension:

1. **Real-time sync**: Trigger PatientInfo updates when OMOP data changes
2. **Data validation**: Built-in validation rules for clinical data
3. **Audit tracking**: Track changes to PatientInfo records
4. **Export functionality**: Export PatientInfo data for external systems
5. **Advanced analytics**: Built-in methods for cohort analysis

## Notes

- All numeric fields support null values for incomplete data
- Unit fields ensure proper interpretation of measurements
- Boolean fields use appropriate defaults for clinical contexts
- JSON fields support complex data structures for genomic information
- The model prioritizes research usability while maintaining clinical accuracy
