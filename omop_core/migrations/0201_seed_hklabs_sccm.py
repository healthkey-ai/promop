"""Seed SourceCodeConceptMapping rows for HK-Labs lab test mappings.

Embeds the same data that ``seed_hklabs_mappings`` and
``import_hklabs_crossmaps`` read from ~/hk-labs JSON files, so that
SCCM rows are created automatically on deploy without requiring the
hk-labs repo on the server.

Sources:
  - loinc_common.json (111 LOINC short names)
  - lab_catalog.json  (37 curated lab display names via _CATALOG_LOINC)
  - curated_aliases_manual.json (8 curated aliases)

Idempotent: uses bulk_create with ignore_conflicts=True.
"""

import re

from django.db import migrations

# ── Normalize function (same as seed_hklabs_mappings._normalize) ─────
def _normalize(text):
    return re.sub(r'[^a-z0-9 ]', '', text.lower()).strip()


# ── Hardcoded data ───────────────────────────────────────────────────
# (source_code_normalized, description, loinc_code)
# Built by deduplicating loinc_common → lab_catalog → curated_aliases,
# keyed on normalized source_code (first occurrence wins).

_LOINC_COMMON = [
    # (short_name, loinc_code, unit)
    ('WBC', '6690-2', '10^3/uL'),
    ('Hemoglobin', '718-7', 'g/dL'),
    ('Platelet count', '777-3', '10^3/uL'),
    ('Neutrophils (abs)', '751-8', '10^3/uL'),
    ('Lymphocytes (abs)', '731-0', '10^3/uL'),
    ('Hematocrit', '4544-3', '%'),
    ('MCV', '787-2', 'fL'),
    ('MCH', '785-6', 'pg'),
    ('MCHC', '786-4', 'g/dL'),
    ('RDW', '788-0', '%'),
    ('RBC count', '789-8', '10^6/uL'),
    ('Platelet MPV', '32623-1', 'fL'),
    ('Monocytes (abs)', '742-7', '10^3/uL'),
    ('Eosinophils (abs)', '711-2', '10^3/uL'),
    ('Basophils (abs)', '704-7', '10^3/uL'),
    ('Neutrophils %', '770-8', '%'),
    ('Lymphocytes %', '736-9', '%'),
    ('Monocytes %', '5905-5', '%'),
    ('Eosinophils %', '713-8', '%'),
    ('Basophils %', '706-2', '%'),
    ('Creatinine', '2160-0', 'mg/dL'),
    ('eGFR (CKD-EPI)', '62238-1', 'mL/min/1.73m2'),
    ('Creatinine clearance', '2164-2', 'mL/min'),
    ('Calcium', '17861-6', 'mg/dL'),
    ('Calcium ionized', '17862-4', 'mg/dL'),
    ('BUN', '3094-0', 'mg/dL'),
    ('Sodium', '2951-2', 'mmol/L'),
    ('Potassium', '2823-3', 'mmol/L'),
    ('Chloride', '2075-0', 'mmol/L'),
    ('Bicarbonate (CO2)', '1963-8', 'mmol/L'),
    ('Glucose', '2345-7', 'mg/dL'),
    ('Glucose fasting', '1558-6', 'mg/dL'),
    ('Glucose fasting plasma', '14749-6', 'mmol/L'),
    ('Phosphorus', '2777-1', 'mg/dL'),
    ('Magnesium', '2601-3', 'mg/dL'),
    ('Iron', '2498-4', 'ug/dL'),
    ('Ferritin', '2276-4', 'ng/mL'),
    ('TIBC', '2500-7', 'ug/dL'),
    ('Transferrin saturation', '2502-3', '%'),
    ('T4 free', '3024-7', 'ng/dL'),
    ('T3 free', '3051-0', 'pg/mL'),
    ('T3 total', '3053-6', 'ng/dL'),
    ('AST', '1920-8', 'U/L'),
    ('ALT', '1742-6', 'U/L'),
    ('Alkaline phosphatase', '6768-6', 'U/L'),
    ('Bilirubin total', '1975-2', 'mg/dL'),
    ('Bilirubin direct', '1968-7', 'mg/dL'),
    ('Albumin', '1751-7', 'g/dL'),
    ('LDH', '2532-0', 'U/L'),
    ('Protein total', '2885-2', 'g/dL'),
    ('Globulin', '10834-0', 'g/dL'),
    ('Amylase', '1798-8', 'U/L'),
    ('Lipase', '1759-0', 'U/L'),
    ('GGT', '2324-2', 'U/L'),
    ('M-protein serum', '33358-3', 'g/dL'),
    ('M-protein urine 24h', '34366-5', 'mg/24h'),
    ('Kappa FLC', '36916-5', 'mg/L'),
    ('Lambda FLC', '33944-0', 'mg/L'),
    ('Kappa/Lambda FLC ratio', '48378-4', ''),
    ('Bone marrow plasma cells', '26450-7', '%'),
    ('Beta-2 microglobulin', '1952-1', 'mg/L'),
    ('IgG', '2458-8', 'mg/dL'),
    ('IgM', '2464-6', 'mg/dL'),
    ('IgA', '2457-0', 'mg/dL'),
    ('CA 15-3', '6875-9', 'U/mL'),
    ('CA 27-29', '17842-6', 'U/mL'),
    ('Ki-67', '85337-4', '%'),
    ('CA 19-9', '10334-1', 'U/mL'),
    ('CA 125', '10335-8', 'U/mL'),
    ('CEA', '2039-6', 'ng/mL'),
    ('AFP', '1834-1', 'ng/mL'),
    ('PSA', '2857-1', 'ng/mL'),
    ('PSA free', '10886-0', 'ng/mL'),
    ('LVEF', '10230-1', '%'),
    ('Troponin I', '10839-9', 'ng/mL'),
    ('Troponin T', '6598-7', 'ng/mL'),
    ('BNP', '30934-4', 'pg/mL'),
    ('NT-proBNP', '33762-6', 'pg/mL'),
    ('Hemoglobin A1c', '4548-4', '%'),
    ('LDL cholesterol (calculated)', '13457-7', 'mg/dL'),
    ('HDL cholesterol', '2085-9', 'mg/dL'),
    ('Cholesterol total', '2093-3', 'mg/dL'),
    ('Triglycerides', '2571-8', 'mg/dL'),
    ('VLDL cholesterol (calculated)', '13458-5', 'mg/dL'),
    ('Cholesterol/HDL ratio', '9830-1', ''),
    ('TSH', '3016-3', 'mIU/L'),
    ('Vitamin D 25-OH', '14635-7', 'ng/mL'),
    ('Vitamin B12', '2132-9', 'pg/mL'),
    ('Folate', '2284-8', 'ng/mL'),
    ('C-reactive protein (CRP)', '1988-5', 'mg/L'),
    ('ESR', '30341-2', 'mm/h'),
    ('Cortisol', '2276-1', 'ug/dL'),
    ('Uric acid', '4024-6', 'mg/dL'),
    ('Haptoglobin', '2695-5', 'mg/dL'),
    ('PT', '5902-2', 's'),
    ('INR', '6301-6', ''),
    ('aPTT', '14979-9', 's'),
    ('Fibrinogen', '3255-7', 'mg/dL'),
    ('D-dimer', '48065-7', 'ug/mL FEU'),
    ('HIV 1/2 antibody', '75622-1', ''),
    ('HBsAg', '5195-3', ''),
    ('HBsAb (anti-HBs)', '5193-8', ''),
    ('HBcAb total', '13952-7', ''),
    ('HCV antibody', '16128-1', ''),
    ('HAV IgM', '22327-9', ''),
    ('Urinalysis protein', '5038-5', ''),
    ('Urinalysis glucose', '5792-7', ''),
    ('Urine specific gravity', '2965-2', ''),
    ('Microalbumin urine', '14957-5', 'mg/L'),
    ('Reticulocyte count', '2639-3', '%'),
]

# lab_catalog abbreviation → LOINC code (same as _CATALOG_LOINC in
# seed_hklabs_mappings.py).
_CATALOG_LOINC = {
    'wbc': '6690-2', 'hgb': '718-7', 'plt': '777-3', 'anc': '751-8',
    'alc': '731-0', 'hct': '4544-3', 'mcv': '787-2',
    'creatinine': '2160-0', 'egfr': '62238-1', 'crcl': '2164-2',
    'calcium': '17861-6', 'bun': '3094-0', 'ast': '1920-8', 'alt': '1742-6',
    'alp': '6768-6', 'bili_total': '1975-2', 'bili_direct': '1968-7',
    'albumin': '1751-7', 'ldh': '2532-0',
    'mspike_serum': '33358-3', 'mspike_urine': '34366-5',
    'flc_kappa': '36916-5', 'flc_lambda': '33944-0', 'flc_ratio': '48378-4',
    'bmpc': '26450-7', 'b2m': '1952-1',
    'ca_15_3': '6875-9', 'ca_27_29': '17842-6', 'ki67': '85337-4',
    'lvef': '10230-1', 'hba1c': '4548-4',
    'ldl': '13457-7', 'hdl': '2085-9', 'tsh': '3016-3',
    'hiv_ab': '75622-1', 'hbsag': '5195-3', 'hcv_ab': '16128-1',
}

# lab_catalog entries: (abbreviation, display_name, name_normalized)
_LAB_CATALOG = [
    ('wbc', 'White blood cell count', 'white blood cell count'),
    ('hgb', 'Hemoglobin', 'hemoglobin'),
    ('plt', 'Platelet count', 'platelet count'),
    ('anc', 'Absolute neutrophil count', 'absolute neutrophil count'),
    ('alc', 'Absolute lymphocyte count', 'absolute lymphocyte count'),
    ('hct', 'Hematocrit', 'hematocrit'),
    ('mcv', 'Mean corpuscular volume', 'mean corpuscular volume'),
    ('creatinine', 'Serum creatinine', 'serum creatinine'),
    ('egfr', 'Estimated GFR (CKD-EPI)', 'estimated gfr ckd epi'),
    ('crcl', 'Creatinine clearance', 'creatinine clearance'),
    ('calcium', 'Serum calcium', 'serum calcium'),
    ('bun', 'Blood urea nitrogen', 'blood urea nitrogen'),
    ('ast', 'Aspartate aminotransferase', 'aspartate aminotransferase'),
    ('alt', 'Alanine aminotransferase', 'alanine aminotransferase'),
    ('alp', 'Alkaline phosphatase', 'alkaline phosphatase'),
    ('bili_total', 'Total bilirubin', 'total bilirubin'),
    ('bili_direct', 'Direct bilirubin', 'direct bilirubin'),
    ('albumin', 'Serum albumin', 'serum albumin'),
    ('ldh', 'Lactate dehydrogenase', 'lactate dehydrogenase'),
    ('mspike_serum', 'Monoclonal protein, serum', 'monoclonal protein serum'),
    ('mspike_urine', 'Monoclonal protein, urine (24h)', 'monoclonal protein urine 24h'),
    ('flc_kappa', 'Kappa free light chain', 'kappa free light chain'),
    ('flc_lambda', 'Lambda free light chain', 'lambda free light chain'),
    ('flc_ratio', 'Kappa/Lambda FLC ratio', 'kappa lambda flc ratio'),
    ('bmpc', 'Bone marrow plasma cells', 'bone marrow plasma cells'),
    ('b2m', 'Serum beta-2 microglobulin', 'serum beta 2 microglobulin'),
    ('ca_15_3', 'CA 15-3', 'ca 15 3'),
    ('ca_27_29', 'CA 27-29', 'ca 27 29'),
    ('ki67', 'Ki-67 proliferation index', 'ki 67 proliferation index'),
    ('lvef', 'Left ventricular ejection fraction', 'left ventricular ejection fraction'),
    ('hba1c', 'Hemoglobin A1c', 'hemoglobin a1c'),
    ('ldl', 'LDL cholesterol', 'ldl cholesterol'),
    ('hdl', 'HDL cholesterol', 'hdl cholesterol'),
    ('tsh', 'Thyroid-stimulating hormone', 'thyroid stimulating hormone'),
    ('hiv_ab', 'HIV 1/2 antibody', 'hiv 1 2 antibody'),
    ('hbsag', 'Hepatitis B surface antigen', 'hepatitis b surface antigen'),
    ('hcv_ab', 'Hepatitis C antibody', 'hepatitis c antibody'),
]

# Curated aliases: (alias, loinc_code)
_CURATED_ALIASES = [
    ('b-Pregnanediol', '110593-1'),
    ('a-Pregnanediol', '110631-9'),
    ('Androsterone', '30509-4'),
    ('Etiocholanolone', '108359-1'),
    ('Cortisone', '30511-0'),
    ('Estradiol (E2)', '13736-4'),
    ('Estrone (E1)', '13739-8'),
    ('Estriol (E3)', '13737-2'),
]


def seed_hklabs_sccm(apps, schema_editor):
    Concept = apps.get_model('omop_core', 'Concept')
    SCCM = apps.get_model('omop_core', 'SourceCodeConceptMapping')

    # Build deduplicated mapping dict keyed by normalized source_code.
    # Order: loinc_common → lab_catalog → curated_aliases (first wins).
    mappings = {}  # normalized_code → (description, loinc_code)

    # 1. LOINC short names
    for short_name, loinc_code, unit in _LOINC_COMMON:
        normalized = _normalize(short_name)[:100]
        if not normalized or normalized in mappings:
            continue
        desc = f'{short_name} ({unit})' if unit else short_name
        mappings[normalized] = (desc, loinc_code)

    # 2. Lab catalog display names
    for abbrev, display_name, name_normalized in _LAB_CATALOG:
        loinc_code = _CATALOG_LOINC.get(abbrev)
        if not loinc_code:
            continue
        normalized = _normalize(name_normalized)[:100]
        if not normalized or normalized in mappings:
            continue
        mappings[normalized] = (display_name, loinc_code)

    # 3. Curated aliases
    for alias, loinc_code in _CURATED_ALIASES:
        normalized = _normalize(alias)[:100]
        if not normalized or normalized in mappings:
            continue
        mappings[normalized] = (alias, loinc_code)

    # Batch-resolve LOINC concepts.
    all_loinc_codes = {loinc_code for _, loinc_code in mappings.values()}
    concepts = {
        c.concept_code: c
        for c in Concept.objects.filter(
            vocabulary_id='LOINC',
            concept_code__in=all_loinc_codes,
        ).only('concept_id', 'concept_code', 'vocabulary_id')
    }

    # Build SCCM rows.
    rows = []
    for src_code, (description, loinc_code) in sorted(mappings.items()):
        target = concepts.get(loinc_code)
        rows.append(SCCM(
            source_vocabulary_id='',
            source_code=src_code,
            source_code_description=description[:255],
            domain_id='Measurement',
            target_concept=target,
            destination_vocabulary_id='LOINC' if target else '',
            omop_table='measurement',
            status='approved' if target else 'proposed',
            origin='import',
            origin_system='hk-labs-seed',
            source='HealthKey',
            occurrence_count=0,
        ))

    SCCM.objects.bulk_create(rows, batch_size=100, ignore_conflicts=True)


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0200_merge_20260901_0000'),
    ]

    operations = [
        migrations.RunPython(seed_hklabs_sccm, migrations.RunPython.noop),
    ]
