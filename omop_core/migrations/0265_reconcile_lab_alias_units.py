"""Repair legacy lab columns from their authoritative canonical PatientRecord fields."""

from decimal import Decimal

from django.db import migrations
from django.db.models import F, Value


def reconcile_aliases(apps, schema_editor):
    records = apps.get_model('omop_core', 'PatientRecord').objects.using(schema_editor.connection.alias)
    # Each update is bounded so a large patient table does not hold one long
    # write lock. Rows without a canonical value may be independent historical
    # entries and are left alone until their normal source is reconciled.
    for canonical, alias, factor, unit_field, unit in (
        ('anc_thousand_per_ul', 'absolute_neutrophile_count', Decimal('1000'),
         'absolute_neutrophile_count_units', 'CELLS/UL'),
        ('alc_thousand_per_ul', 'absolute_lymphocyte_count', 1000, None, None),
        ('hemoglobin_g_dl', 'hemoglobin_level', None, 'hemoglobin_level_units', 'G/DL'),
    ):
        sources = records.filter(**{f'{canonical}__isnull': False}).exclude(
            user_edited_fields__contains=[alias],
        )
        last_pk = 0
        while True:
            ids = list(sources.filter(pk__gt=last_pk).order_by('pk').values_list('pk', flat=True)[:1000])
            if not ids:
                break
            values = {alias: F(canonical) * Value(factor) if factor is not None else F(canonical)}
            if unit_field:
                values[unit_field] = unit
            records.filter(pk__in=ids).update(**values)
            last_pk = ids[-1]


class Migration(migrations.Migration):
    atomic = False
    dependencies = [('omop_core', '0264_concept_source_curation')]
    operations = [migrations.RunPython(reconcile_aliases, migrations.RunPython.noop, atomic=False)]
