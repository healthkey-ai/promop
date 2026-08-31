"""Represent clinician-validation audit fields as Person equivalences.

The values are administrative attributes on Person, copied directly into the
PatientRecord projection.  They are not clinical facts, so a Concept mapping
(in particular the generic SNOMED "Confirmed" suggestion) is misleading.
The active formulas make the direct, administrator-editable equivalence visible
in the field mapper.
"""

from django.db import migrations


PERSON_EQUIVALENCE_FORMULAS = {
    'validated': 'validated',
    'validated_by': 'validated_by',
    'validation_date': 'validation_date',
}


def seed_person_equivalence_formulas(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')

    # These fields have no independent OMOP meaning.  Remove any past proposed
    # or approved mapping rather than leaving an obsolete curation decision.
    FieldConceptMapping.objects.filter(
        field_name__in=PERSON_EQUIVALENCE_FORMULAS,
    ).delete()

    for field_name, formula in PERSON_EQUIVALENCE_FORMULAS.items():
        # Do not overwrite a formula an administrator has intentionally edited.
        FieldFormula.objects.get_or_create(
            field_name=field_name,
            defaults={'formula': formula, 'is_active': True},
        )


def unseed_person_equivalence_formulas(apps, schema_editor):
    FieldFormula = apps.get_model('omop_core', 'FieldFormula')
    for field_name, formula in PERSON_EQUIVALENCE_FORMULAS.items():
        FieldFormula.objects.filter(field_name=field_name, formula=formula).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0177_merge_20260827_1205'),
    ]

    operations = [
        migrations.RunPython(
            seed_person_equivalence_formulas,
            unseed_person_equivalence_formulas,
        ),
    ]
