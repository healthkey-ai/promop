"""Populate blank curated mapping units from the US-oriented field defaults."""

from django.db import migrations


def backfill_units(apps, schema_editor):
    # Import at execution time so the curated unit source remains in one place.
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
    dependencies = [('omop_core', '0170_make_viral_inverse_fields_nullable')]

    operations = [migrations.RunPython(backfill_units, migrations.RunPython.noop)]
