"""Backfill curated mapping units for newly added FIELD_COMMON_UNITS entries."""

from django.db import migrations


def backfill_units(apps, schema_editor):
    # Same frozen defaults as 0171; see that migration.
    from omop_core.migrations._field_common_units import DEFAULT_UNITS

    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for field_name, unit in DEFAULT_UNITS.items():
        FieldConceptMapping.objects.filter(
            field_name=field_name,
            unit='',
            status__in=('proposed', 'approved'),
        ).update(unit=unit)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0171_backfill_field_mapping_units')]

    operations = [migrations.RunPython(backfill_units, migrations.RunPython.noop)]
