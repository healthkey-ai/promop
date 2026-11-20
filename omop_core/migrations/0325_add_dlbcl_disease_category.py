"""Add DLBCL as a disease category and remap its entries from Other.

DLBCL (Diffuse Large B-Cell Lymphoma) has 88 patients on staging and is
clinically distinct enough to warrant its own category rather than being
lumped under Other.
"""

from django.db import migrations


# New category to add.
_NEW_CATEGORY = ('DLBCL', 'Diffuse Large B-Cell Lymphoma')

# Disease names to remap from Other → DLBCL in DiseaseCategoryMapping.
_REMAP = [
    'Diffuse large B-cell lymphoma',
]


def forward(apps, schema_editor):
    Choice = apps.get_model('omop_core', 'DiseaseCategoryChoice')
    Mapping = apps.get_model('omop_core', 'DiseaseCategoryMapping')

    # 1. Add the new choice row.
    Choice.objects.get_or_create(code=_NEW_CATEGORY[0], defaults={'label': _NEW_CATEGORY[1]})

    # 2. Remap known DLBCL disease names from Other to DLBCL.
    Mapping.objects.filter(disease_name__in=_REMAP, category='Other').update(category='DLBCL')


def reverse(apps, schema_editor):
    Choice = apps.get_model('omop_core', 'DiseaseCategoryChoice')
    Mapping = apps.get_model('omop_core', 'DiseaseCategoryMapping')

    # Revert mappings back to Other.
    Mapping.objects.filter(disease_name__in=_REMAP, category='DLBCL').update(category='Other')

    # Remove the choice row (only if we created it).
    Choice.objects.filter(code=_NEW_CATEGORY[0]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0324_patientrecord_disease_slugs'),
    ]

    operations = [
        migrations.RunPython(forward, reverse),
    ]
