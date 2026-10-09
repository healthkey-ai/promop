"""Backfill curated mapping units for 3 blood count fields added in PR #731."""

from django.db import migrations

_FIELDS = {
    'absolute_neutrophile_count': '10*3/uL',
    'platelet_count': '10*3/uL',
    'red_blood_cell_count': '10*6/uL',
}


def backfill_units(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    for field_name, default_unit in _FIELDS.items():
        FieldConceptMapping.objects.filter(
            field_name=field_name,
            unit='',
            status__in=('proposed', 'approved'),
        ).update(unit=default_unit)


class Migration(migrations.Migration):
    dependencies = [('omop_core', '0172_backfill_extended_field_units')]

    operations = [migrations.RunPython(backfill_units, migrations.RunPython.noop)]
