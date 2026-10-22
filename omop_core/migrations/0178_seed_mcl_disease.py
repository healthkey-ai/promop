"""Seed Mantle Cell Lymphoma in the Disease vocabulary.

Every other disease was seeded via migration 0056 and had its code set in 0057.
MCL is new to the therapy reference tables and must be present before the
disease_therapy_rounds.csv loader runs, so it needs a migration — the loader's
get_or_create is a safety net, not the authoritative source.
"""
from django.db import migrations


def seed_mcl(apps, schema_editor):
    Disease = apps.get_model('omop_core', 'Disease')
    Disease.objects.get_or_create(code='MCL', defaults={'title': 'Mantle Cell Lymphoma'})


def unseed_mcl(apps, schema_editor):
    Disease = apps.get_model('omop_core', 'Disease')
    Disease.objects.filter(code='MCL').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0177_merge_20260827_1205'),
    ]

    operations = [
        migrations.RunPython(seed_mcl, unseed_mcl),
    ]
