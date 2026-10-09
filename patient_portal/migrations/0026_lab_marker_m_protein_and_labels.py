"""M-protein, and readable names for the seeded lab markers.

0022 seeded the ranking without M-protein (serum protein electrophoresis,
LOINC 33358-3), myeloma's main marker, and without labels, so the record
showed LOINC names such as "Kappa light chains.free/Lambda light
chains.free". This adds M-protein if it is missing and fills a label only
where it is blank: rows the clinical admins have edited stay as they are.
"""
from django.db import migrations

MYELOMA = ['myeloma', 'multiple-myeloma']

M_PROTEIN = ('33358-3', 1, [], MYELOMA, 'M-protein')

LABELS = {
    '48378-4': 'Kappa/lambda ratio',
    '2857-1': 'PSA',
    '36916-5': 'Kappa free light chain',
    '33944-0': 'Lambda free light chain',
    '6875-9': 'CA 15-3',
    '2039-6': 'CEA',
    '25390-6': 'CA 19-9',
    '1952-1': 'Beta-2 microglobulin',
    '2465-3': 'IgG',
    '2458-8': 'IgA',
    '2472-9': 'IgM',
    '6690-2': 'White blood cells',
    '718-7': 'Hemoglobin',
    '777-3': 'Platelets',
    '751-8': 'Neutrophils',
    '731-0': 'Lymphocytes',
    '742-7': 'Monocytes',
    '789-8': 'Red blood cells',
    '20570-8': 'Hematocrit',
    '4544-3': 'Hematocrit',
    '787-2': 'MCV',
    '785-6': 'MCH',
    '786-4': 'MCHC',
    '788-0': 'RDW',
    '2160-0': 'Creatinine',
    '17861-6': 'Calcium',
    '3094-0': 'BUN',
    '2951-2': 'Sodium',
    '2823-3': 'Potassium',
    '2075-0': 'Chloride',
    '2028-9': 'CO2',
    '2345-7': 'Glucose',
    '1751-7': 'Albumin',
    '2885-2': 'Total protein',
    '6768-6': 'Alkaline phosphatase',
    '1742-6': 'ALT',
    '1920-8': 'AST',
    '1975-2': 'Total bilirubin',
    '2532-0': 'LDH',
    '1988-5': 'CRP',
}


def fill(apps, schema_editor):
    LabMarker = apps.get_model('patient_portal', 'LabMarker')
    code, rank, panels, slugs, label = M_PROTEIN
    LabMarker.objects.get_or_create(
        loinc_code=code, defaults={'rank': rank, 'panels': panels, 'disease_slugs': slugs, 'label': label},
    )
    for marker in LabMarker.objects.filter(loinc_code__in=LABELS, label=''):
        marker.label = LABELS[marker.loinc_code]
        marker.save(update_fields=['label'])


class Migration(migrations.Migration):
    dependencies = [('patient_portal', '0025_ai_explanation')]
    operations = [migrations.RunPython(fill, migrations.RunPython.noop)]
