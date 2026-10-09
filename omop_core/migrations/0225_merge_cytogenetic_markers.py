"""Join current dev and repair any earlier development cytogenetic seeds."""

from importlib import import_module

from django.db import migrations


def correct_cytogenetic_codes(apps, schema_editor):
    # Historical, frozen implementation; never import the live service here.
    migration = import_module('omop_core.migrations.0222_cytogenetic_markers')
    migration.migrate_and_seed(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0222_cytogenetic_markers'),
        ('omop_core', '0224_field_mapping_provenance'),
    ]

    operations = [
        # Removing incorrect coding is intentionally not undone on rollback.
        migrations.RunPython(correct_cytogenetic_codes, migrations.RunPython.noop),
    ]
