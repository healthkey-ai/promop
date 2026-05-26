"""Backfill measurement_source_value with original test names.

Prior to this change, LOINC-matched measurements stored the match_method
(e.g. "loinc", "alias_exact") in measurement_source_value instead of the
original test name.  This migration replaces those values with the human-
readable test name so the Lab Results UI can display it.
"""
from django.db import migrations


# LOINC concept_code → original test name (from hk-labs LabTestEntry)
_LOINC_TO_NAME = {
    "1756-6": "Alb CSF/SerPl",
    "1751-7": "Albumin",
    "6768-6": "Alkaline phosphatase",
    "1742-6": "ALT",
    "5949-3": "aPTT imm NP Cont PPP Cont",
    "1920-8": "AST",
    "704-7": "Basophils (abs)",
    "706-2": "Basophils %",
    "1963-8": "Bicarbonate (CO2)",
    "1976-0": "Bilirub Stl Ql",
    "1975-2": "Bilirubin total",
    "3094-0": "BUN",
    "17861-6": "Calcium",
    "2075-0": "Chloride",
    "2093-3": "Cholesterol total",
    "2161-8": "Creat Ur-mCnc",
    "2160-0": "Creatinine",
    "62238-1": "eGFR (CKD-EPI)",
    "711-2": "Eosinophils (abs)",
    "713-8": "Eosinophils %",
    "10834-0": "Globulin",
    "2345-7": "GLUCOSE",
    "2342-4": "Glucose CSF-mCnc",
    "1558-6": "Glucose fasting",
    "2085-9": "HDL Cholesterol",
    "4544-3": "Hematocrit",
    "718-7": "Hemoglobin",
    "4548-4": "Hemoglobin A1c",
    "2514-8": "Ketones Ur Ql Strip",
    "13457-7": "LDL cholesterol (calculated)",
    "11054-4": "LDLc/HDLc SerPl",
    "731-0": "Lymphocytes (abs)",
    "736-9": "Lymphocytes %",
    "785-6": "MCH",
    "786-4": "MCHC",
    "787-2": "MCV",
    "14957-5": "Microalbumin urine",
    "14959-1": "Microalbumin/Creat Ur",
    "742-7": "Monocytes (abs)",
    "5905-5": "Monocytes %",
    "751-8": "Neutrophils (abs)",
    "770-8": "Neutrophils %",
    "43396-1": "NonHDLc SerPl-mCnc",
    "777-3": "Platelet count",
    "32623-1": "Platelet MPV",
    "2823-3": "Potassium",
    "2888-6": "Prot Ur-mCnc",
    "2885-2": "Protein total",
    "789-8": "RBC count",
    "788-0": "RDW",
    "2951-2": "Sodium",
    "3051-0": "T3 free",
    "3053-6": "T3 total",
    "3024-7": "T4 free",
    "8098-6": "Thyroglob Ab SerPl-aCnc",
    "2571-8": "Triglycerides",
    "3016-3": "TSH",
    "11580-8": "TSH SerPl DL<=0.005 mIU/L-aCnc",
    "2132-9": "Vitamin B12",
    "13458-5": "VLDL cholesterol (calculated)",
    "6690-2": "WBC",
}

_STALE_VALUES = ("loinc", "alias_exact", "name_fallback", "manual", "unmatched")


def backfill(apps, schema_editor):
    Concept = apps.get_model("omop_core", "Concept")
    Measurement = apps.get_model("omop_core", "Measurement")

    for loinc_code, test_name in _LOINC_TO_NAME.items():
        concept = Concept.objects.filter(
            concept_code=loinc_code, vocabulary_id="LOINC",
        ).first()
        if not concept:
            continue
        Measurement.objects.filter(
            measurement_concept_id=concept.concept_id,
            measurement_source_value__in=_STALE_VALUES,
        ).update(measurement_source_value=test_name[:50])


class Migration(migrations.Migration):
    dependencies = [
        ("omop_core", "0077_seed_concept_zero"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
