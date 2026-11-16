"""A `{...}` marker in `FieldConceptMapping.unit` is not a unit (#2605).

`field_curation_v1.json` spells "this value has no physical unit" as `{score}`,
`{INR}` or `{ratio}`, and the descriptor copied the marker verbatim into `unit`
and `projection.unit` — where the client printed it beside the edit box. The
descriptor drops placeholders at every boundary now; this clears the stored
copies so no path that bypasses the mapping form can bring one back.
"""

from django.db import migrations


def clear_placeholder_units(apps, schema_editor):
    FieldConceptMapping = apps.get_model('omop_core', 'FieldConceptMapping')
    FieldConceptMapping.objects.filter(
        unit__startswith='{', unit__endswith='}',
    ).update(unit='')


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0343_merge_dev_sync_20261114'),
    ]

    operations = [
        # The placeholder carried no information the blank does not: it never
        # named a unit, and nothing read it except the descriptor that leaked
        # it. Rolling back cannot know which rows held it, so it restores
        # nothing.
        migrations.RunPython(clear_placeholder_units, migrations.RunPython.noop),
    ]
