"""Seed OMOP concept_id=0 ("No matching concept") sentinel.

Every OMOP CDM installation requires concept 0 as the default for
measurement_concept_id when no standard concept maps to the source data.
The Athena vocabulary bundle does not always include it, so we ensure it
exists here.
"""
from django.db import migrations


def seed_concept_zero(apps, schema_editor):
    Vocabulary = apps.get_model("omop_core", "Vocabulary")
    Domain = apps.get_model("omop_core", "Domain")
    ConceptClass = apps.get_model("omop_core", "ConceptClass")
    Concept = apps.get_model("omop_core", "Concept")

    Vocabulary.objects.get_or_create(
        vocabulary_id="None",
        defaults={"vocabulary_name": "None", "vocabulary_concept_id": 0},
    )
    Domain.objects.get_or_create(
        domain_id="Metadata",
        defaults={"domain_name": "Metadata", "domain_concept_id": 0},
    )
    ConceptClass.objects.get_or_create(
        concept_class_id="Undefined",
        defaults={"concept_class_name": "Undefined", "concept_class_concept_id": 0},
    )
    Concept.objects.get_or_create(
        concept_id=0,
        defaults={
            "concept_name": "No matching concept",
            "domain_id": "Metadata",
            "vocabulary_id": "None",
            "concept_class_id": "Undefined",
            "concept_code": "No matching concept",
            "valid_start_date": "1970-01-01",
            "valid_end_date": "2099-12-31",
        },
    )


class Migration(migrations.Migration):
    dependencies = [
        ("omop_core", "0076_expand_loinc_class_code"),
    ]

    operations = [
        migrations.RunPython(seed_concept_zero, migrations.RunPython.noop),
    ]
