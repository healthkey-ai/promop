"""Seed HealthTree's first chronic-condition override (#2566).

AHRQ's indicator calls osteopenia not chronic, which is wrong for myeloma, so
M85.8 ("other specified disorders of bone density and structure", where
osteopenia is indexed) is forced chronic. get_or_create: a replay, or an
instance whose curators already entered the code, keeps their row.
"""
from django.db import migrations


def seed(apps, schema_editor):
    Override = apps.get_model('omop_core', 'ChronicConditionOverride')
    Override.objects.get_or_create(code='M85.8', defaults={
        'is_chronic': True,
        'label': 'Osteopenia',
        'notes': 'AHRQ CCIR says not chronic; it is for myeloma patients.',
    })


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0341_chronic_condition_override'),
    ]

    operations = [
        # Rolling back drops the table with 0341, so there is nothing to undo here.
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
