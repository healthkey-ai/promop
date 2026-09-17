"""Backfill provenance on legacy FieldConceptMapping rows.

Every FCM row seeded by data migrations 0156–0227 has blank provenance because
the field was added in migration 0224.  All blank-provenance rows were created
by migrations (none through the curator UI), so they are ``system_generated``.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    FCM = apps.get_model('omop_core', 'FieldConceptMapping')
    updated = FCM.objects.filter(provenance='').update(
        provenance='system_generated',
    )
    if updated:
        print(f'  Backfilled provenance on {updated} field mapping(s)')


def reverse(apps, schema_editor):
    # Cannot distinguish which rows were blank before vs. legitimately
    # system_generated.  Reverse is a no-op; the blank state carried no
    # information.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0238_loinc_code_class_example_units'),
    ]

    operations = [
        migrations.RunPython(backfill, reverse, elidable=True),
    ]
