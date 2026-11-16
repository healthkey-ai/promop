"""Seed each disease's extension from the fields its tab renders today (#2331).

MM, BC, FL, MCL and CLL start with the field sets of
``disease_profiles.SEED_DISEASE_FIELDS``, which mirror ``DiseaseTab.tsx``.
Every other field has no row and is part of the base record. Additive: a row
already present is left alone, and a category this instance's curators
deleted gets no members. Read-only as far as patients go: no tab and no save
consults these rows yet.
"""
from django.db import migrations


def seed(apps, schema_editor):
    from omop_core.services import disease_profiles

    disease_profiles.seed(
        apps.get_model('omop_core', 'FieldDiseaseMembership'),
        apps.get_model('omop_core', 'DiseaseCategoryChoice'),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0343_field_disease_membership'),
    ]

    operations = [
        # Rolling back drops the table with 0343, so there is nothing to undo here.
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
