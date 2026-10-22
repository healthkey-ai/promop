"""Seed FieldChoice records for all dropdown fields in the patient info UI.

Values are drawn from the frontend's patientConstants.ts and CancerBot's field
definitions.  Fields that already have choices (from migration 0162) are skipped
via get_or_create so this is safe to re-run.

Closes #1112.
"""

from django.db import migrations


# field_name → [(display, [(code, vocabulary_id, code_display, is_primary)])]
#
# Fields already seeded in 0162: disease, smoking_status, alcohol_use,
# menopausal_status, exercise_frequency, insurance_type, employment_status.
# Those are NOT repeated here.

DROPDOWN_CHOICES = {
    # ── Breast Cancer ────────────────────────────────────────────────────
    'histologic_type': [
        ('Unknown', []),
        ('Infiltrating ductal carcinoma (IDC)', [('82711006', 'SNOMED', 'Infiltrating duct carcinoma', True)]),
        ('Ductal carcinoma in situ (DCIS)', [('399294002', 'SNOMED', 'Ductal carcinoma in situ', True)]),
        ('Infiltrating lobular carcinoma (ILC)', [('443460007', 'SNOMED', 'Infiltrating lobular carcinoma', True)]),
        ('Lobular carcinoma in situ (LCIS)', [('77284006', 'SNOMED', 'Lobular carcinoma in situ', True)]),
        ('Mixed ductal and lobular carcinoma', []),
        ('Mucinous (colloid) carcinoma', [('72495009', 'SNOMED', 'Mucinous adenocarcinoma', True)]),
        ('Tubular carcinoma', [('21845001', 'SNOMED', 'Tubular adenocarcinoma', True)]),
        ('Medullary carcinoma', [('32913002', 'SNOMED', 'Medullary carcinoma', True)]),
        ('Papillary carcinoma', [('4079000', 'SNOMED', 'Papillary carcinoma', True)]),
        ('Metaplastic carcinoma', []),
        ('Paget disease of the nipple', [('22049009', 'SNOMED', 'Paget disease of breast', True)]),
        ('Inflammatory carcinoma', [('266895008', 'SNOMED', 'Inflammatory carcinoma of breast', True)]),
    ],
    'tumor_stage': [
        ('Tx: Primary Tumor, cannot be assessed', []),
        ('T0: No tumor evidence', []),
        ('Tis: Non-invasive Carcinoma in situ', []),
        ('T1: Invasive Tumor <= 2 cm', []),
        ('T1mi: Invasive Tumor <= 0.1 cm', []),
        ('T1a: 0.1 - 0.5 cm', []),
        ('T1b: 0.5 - 1 cm', []),
        ('T1c: 1 - 2 cm', []),
        ('T2: Invasive Tumor > 2 - 5 cm', []),
        ('T3: Invasive Tumor > 5 cm', []),
        ('T4: Invades chest wall or skin, or inflammatory', []),
        ('T4a: Invades chest wall', []),
        ('T4b: Invades skin', []),
        ('T4c: Invades both skin + chest wall', []),
        ('T4d: Inflammatory carcinoma', []),
    ],
    'nodes_stage': [
        ('NX: Nodes cannot be assessed', []),
        ('N0: No lymph node involvement', []),
        ('N1: 1-3 axillary lymph nodes', []),
        ('N1mi: Micrometastasis (0.2-2 mm)', []),
        ('N1a: 1-3 axillary nodes (>2 mm)', []),
        ('N1b: Cancer cells in internal mammary sentinel nodes', []),
        ('N1c: 1-3 axillary nodes + internal mammary sentinel nodes', []),
        ('N2: 4-9 axillary nodes', []),
        ('N2a: 4-9 axillary nodes (>2 mm)', []),
        ('N2b: Internal mammary nodes only', []),
        ('N3: 10+ axillary nodes', []),
        ('N3a: >=10 axillary nodes or infraclavicular', []),
        ('N3b: 4-9 axillary + mammary nodes', []),
        ('N3c: Supraclavicular nodes', []),
    ],
    'distant_metastasis_stage': [
        ('M0: No distant metastasis', []),
        ('M0(i+): No metastasis on scans, but cancer cells found', []),
        ('M1: Distant metastasis present', []),
    ],
    'staging_modalities': [
        ('c: Clinical', []),
        ('p: Pathological', []),
        ('yp: Pathological after neoadjuvant therapy', []),
    ],
    'estrogen_receptor_status': [
        ('Positive', [('416053008', 'SNOMED', 'Estrogen receptor positive tumor', True)]),
        ('Negative', [('416237000', 'SNOMED', 'Estrogen receptor negative neoplasm', True)]),
        ('Equivocal', []),
        ('Unknown', []),
    ],
    'progesterone_receptor_status': [
        ('Positive', [('416561008', 'SNOMED', 'Progesterone receptor positive tumor', True)]),
        ('Negative', [('441117001', 'SNOMED', 'Progesterone receptor negative neoplasm', True)]),
        ('Equivocal', []),
        ('Unknown', []),
    ],
    'her2_status': [
        ('Positive', [('431396003', 'SNOMED', 'Human epidermal growth factor 2 positive carcinoma of breast', True)]),
        ('Negative', []),
        ('Equivocal', []),
        ('Unknown', []),
    ],
    'hr_status': [
        ('HR+', []),
        ('HR-', []),
        ('HR+ with low expression', []),
        ('HR+ with high expression', []),
        ('Unknown', []),
    ],
    'hrd_status': [
        ('HRD+', []),
        ('HRD-', []),
        ('Unknown', []),
    ],
    'androgen_receptor_status': [
        ('Positive', []),
        ('Negative', []),
        ('Equivocal', []),
        ('Unknown', []),
    ],

    # ── Follicular Lymphoma ──────────────────────────────────────────────
    'stage': [
        ('0', []),
        ('I', []),
        ('IA', []),
        ('IB', []),
        ('II', []),
        ('IIA', []),
        ('IIB', []),
        ('III', []),
        ('IIIA', []),
        ('IIIB', []),
        ('IIIC', []),
        ('IV', []),
        ('Unknown', []),
    ],
    'tumor_grade': [
        ('Grade 1 (0-5 centroblasts/HPF)', []),
        ('Grade 2 (6-15 centroblasts/HPF)', []),
        ('Grade 3a (>15 centroblasts/HPF, centrocytes present)', []),
        ('Grade 3b (solid sheets of centroblasts)', []),
    ],
    'gelf_criteria_status': [
        ('Met', []),
        ('Not Met', []),
        ('Unknown', []),
    ],
    'flipi_risk_category': [
        ('Low', []),
        ('Intermediate', []),
        ('High', []),
    ],
    'bulky_disease': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],
    'b_symptoms': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],
    'bone_marrow_involvement': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],

    # ── Multiple Myeloma ─────────────────────────────────────────────────
    'myeloma_type': [
        ('IgG kappa', []),
        ('IgG lambda', []),
        ('IgA kappa', []),
        ('IgA lambda', []),
        ('IgD kappa', []),
        ('IgD lambda', []),
        ('IgE kappa', []),
        ('IgE lambda', []),
        ('IgM kappa', []),
        ('IgM lambda', []),
        ('Light-chain kappa', []),
        ('Light-chain lambda', []),
    ],
    'r_iss_stage': [
        ('Stage I', []),
        ('Stage II', []),
        ('Stage III', []),
    ],
    'progression': [
        ('Stable', []),
        ('Active', []),
        ('Smoldering', []),
        ('Progressive', []),
        ('Relapsed', []),
        ('Refractory', []),
    ],
    'mrd_status': [
        ('MRD Negative (10\u207b\u2075)', []),
        ('MRD Negative (10\u207b\u2076)', []),
        ('Sustained MRD Negative', []),
        ('MRD Positive', []),
        ('Not Assessed', []),
        ('Unknown', []),
    ],
    'bone_lesions': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],
    'hypercalcemia': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],
    'renal_impairment': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],
    'anemia': [
        ('Yes', []),
        ('No', []),
        ('Unknown', []),
    ],
    'cytogenetic_risk': [
        ('Standard Risk', []),
        ('High Risk', []),
        ('Very High Risk', []),
    ],

    # ── CLL ──────────────────────────────────────────────────────────────
    'binet_stage': [
        ('Binet Stage A (<3 lymphoid areas involved)', []),
        ('Binet Stage B (>=3 lymphoid areas involved)', []),
        ('Binet Stage C (Anemia or Thrombocytopenia)', []),
    ],
    'protein_expressions': [
        ('CD38 +ve', []),
        ('CD38 -ve', []),
        ('ZAP-70 +ve', []),
        ('ZAP-70 -ve', []),
        ('CD49d +ve', []),
        ('CD49d -ve', []),
        ('CD19 +ve', []),
        ('CD19 -ve', []),
        ('CD5 +ve', []),
        ('CD5 -ve', []),
        ('CD20 +ve', []),
        ('CD20 -ve', []),
        ('CD23 +ve', []),
        ('CD23 -ve', []),
        ('Kappa light chain +ve', []),
        ('Kappa light chain -ve', []),
        ('Lambda light chain +ve', []),
        ('Lambda light chain -ve', []),
    ],
    'richter_transformation': [
        ('Richter Transformation to DLBCL', []),
        ('Richter Transformation to Hodgkin Lymphoma', []),
        ('Richter Transformation to Non-Hodgkin Lymphoma', []),
        ('Clonally Related RT', []),
        ('Clonally Unrelated RT', []),
    ],
    'tumor_burden': [
        ('Low', []),
        ('Intermediate', []),
        ('High', []),
    ],
    'disease_activity': [
        ('Active', []),
        ('Inactive', []),
        ('Remission', []),
        ('Relapsed', []),
        ('Refractory', []),
    ],

    # ── Behavior / Socioeconomic ─────────────────────────────────────────
    'diet_type': [
        ('Regular', []),
        ('Vegetarian', []),
        ('Vegan', []),
        ('Mediterranean', []),
        ('Low-carb', []),
        ('Ketogenic', []),
        ('Other', []),
    ],
    'sleep_quality': [
        ('Excellent', []),
        ('Good', []),
        ('Fair', []),
        ('Poor', []),
        ('Very Poor', []),
    ],
    'stress_level': [
        ('None', []),
        ('Low', []),
        ('Moderate', []),
        ('High', []),
        ('Very High', []),
    ],
    'social_support': [
        ('Excellent', []),
        ('Good', []),
        ('Fair', []),
        ('Poor', []),
        ('None', []),
    ],
    'education_level': [
        ('Less than High School', []),
        ('High School Graduate', []),
        ('Some College', []),
        ('Associate Degree', []),
        ('Bachelor Degree', []),
        ('Master Degree', []),
        ('Doctoral Degree', []),
        ('Professional Degree', []),
    ],
    'marital_status': [
        ('Single', []),
        ('Married', []),
        ('Divorced', []),
        ('Widowed', []),
        ('Separated', []),
        ('Domestic Partnership', []),
    ],
}


def seed_choices(apps, schema_editor):
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    FieldChoiceCode = apps.get_model('omop_core', 'FieldChoiceCode')
    for field_name, entries in DROPDOWN_CHOICES.items():
        for sort_order, (display, codes) in enumerate(entries):
            choice, _ = FieldChoice.objects.get_or_create(
                field_name=field_name,
                display=display,
                defaults={'sort_order': sort_order},
            )
            for code, vocab, code_display, is_primary in codes:
                FieldChoiceCode.objects.get_or_create(
                    choice=choice,
                    vocabulary_id=vocab,
                    code=code,
                    defaults={
                        'display': code_display,
                        'is_primary': is_primary,
                    },
                )


def reverse_choices(apps, schema_editor):
    FieldChoice = apps.get_model('omop_core', 'FieldChoice')
    for field_name in DROPDOWN_CHOICES:
        displays = [display for display, _ in DROPDOWN_CHOICES[field_name]]
        FieldChoice.objects.filter(
            field_name=field_name, display__in=displays,
        ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0216_remove_icd10_athena_duplicates'),
    ]
    operations = [
        migrations.RunPython(seed_choices, reverse_choices),
    ]
