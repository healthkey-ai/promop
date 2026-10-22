"""Backfill curated mapping units for newly added FIELD_COMMON_UNITS entries."""

from django.db import migrations


def backfill_units(apps, schema_editor):
    from omop_core.services.mappings import FIELD_COMMON_UNITS

    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for field_name, units in FIELD_COMMON_UNITS.items():
        if units:
            FieldConceptMapping.objects.filter(
                field_name=field_name,
                unit='',
                status__in=('proposed', 'approved'),
            ).update(unit=units[0])


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0171_backfill_field_mapping_units')]

    operations = [migrations.RunPython(backfill_units, migrations.RunPython.noop)]
