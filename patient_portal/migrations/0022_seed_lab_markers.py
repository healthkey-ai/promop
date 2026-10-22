"""Default PHR lab ranking until the CureHub observation labels are imported.

Disease markers first, then CBC, then the metabolic panels, LDH and CRP —
the order the PRD describes. Admins edit these in the Django admin; this seed
only fills an empty table and never overwrites their changes.
"""
from django.db import migrations

MYELOMA = ['myeloma', 'multiple-myeloma']
LYMPHOID = ['chronic-lymphocytic-leukemia', 'follicular-lymphoma']

MARKERS = [
    # (loinc, rank, panels, disease slugs)
    ('48378-4', 1, [], MYELOMA),            # Kappa/Lambda free light chain ratio
    ('2857-1', 1, [], ['prostate-cancer']),  # PSA
    ('36916-5', 2, [], MYELOMA),            # Kappa free light chains
    ('33944-0', 3, [], MYELOMA),            # Lambda free light chains
    ('6875-9', 3, [], ['breast-cancer']),    # CA 15-3
    ('2039-6', 4, [], ['breast-cancer', 'colon-cancer', 'lung-cancer']),  # CEA
    ('25390-6', 4, [], ['pancreatic-cancer']),  # CA 19-9
    ('1952-1', 5, [], MYELOMA + LYMPHOID),  # Beta-2 microglobulin
    ('2465-3', 6, [], MYELOMA),             # IgG
    ('2458-8', 7, [], MYELOMA),             # IgA
    ('2472-9', 8, [], MYELOMA),             # IgM
    ('6690-2', 10, ['CBC'], []),            # WBC
    ('718-7', 11, ['CBC'], []),             # Hemoglobin
    ('777-3', 12, ['CBC'], []),             # Platelets
    ('751-8', 13, ['CBC'], []),             # Neutrophils (ANC)
    ('731-0', 14, ['CBC'], []),             # Lymphocytes (ALC)
    ('742-7', 15, ['CBC'], []),             # Monocytes
    ('789-8', 16, ['CBC'], []),             # RBC
    ('20570-8', 17, ['CBC'], []),           # Hematocrit
    ('4544-3', 17, ['CBC'], []),            # Hematocrit (automated count)
    ('787-2', 18, ['CBC'], []),             # MCV
    ('785-6', 19, ['CBC'], []),             # MCH
    ('786-4', 20, ['CBC'], []),             # MCHC
    ('788-0', 21, ['CBC'], []),             # RDW
    ('2160-0', 30, ['BMP', 'CMP'], []),     # Creatinine
    ('17861-6', 31, ['BMP', 'CMP'], []),    # Calcium
    ('3094-0', 32, ['BMP', 'CMP'], []),     # BUN
    ('2951-2', 33, ['BMP', 'CMP'], []),     # Sodium
    ('2823-3', 34, ['BMP', 'CMP'], []),     # Potassium
    ('2075-0', 35, ['BMP', 'CMP'], []),     # Chloride
    ('2028-9', 36, ['BMP', 'CMP'], []),     # CO2
    ('2345-7', 37, ['BMP', 'CMP'], []),     # Glucose
    ('1751-7', 38, ['CMP'], []),            # Albumin
    ('2885-2', 39, ['CMP'], []),            # Total protein
    ('6768-6', 40, ['CMP'], []),            # Alkaline phosphatase
    ('1742-6', 41, ['CMP'], []),            # ALT
    ('1920-8', 42, ['CMP'], []),            # AST
    ('1975-2', 43, ['CMP'], []),            # Total bilirubin
    ('2532-0', 50, [], LYMPHOID),           # LDH
    ('1988-5', 51, [], []),                 # CRP
]


def seed(apps, schema_editor):
    LabMarker = apps.get_model('patient_portal', 'LabMarker')
    if LabMarker.objects.exists():
        return
    LabMarker.objects.bulk_create(
        LabMarker(loinc_code=code, rank=rank, panels=panels, disease_slugs=slugs)
        for code, rank, panels, slugs in MARKERS
    )


class Migration(migrations.Migration):
    dependencies = [('patient_portal', '0021_lab_marker')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
