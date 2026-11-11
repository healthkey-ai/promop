"""Populate blank curated mapping units from the US-oriented field defaults."""

from django.db import migrations


def backfill_units(apps, schema_editor):
    # Frozen data, not the live service: `omop_core.services.mappings` imports
    # the current Concept model, which a fresh migrate must not depend on.
    from omop_core.migrations._field_common_units import DEFAULT_UNITS

    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for field_name, unit in DEFAULT_UNITS.items():
        FieldConceptMapping.objects.filter(
            field_name=field_name,
            unit='',
            status__in=('proposed', 'approved'),
        ).update(unit=unit)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0170_make_viral_inverse_fields_nullable')]

    operations = [migrations.RunPython(backfill_units, migrations.RunPython.noop)]
