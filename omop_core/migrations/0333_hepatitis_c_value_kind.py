"""Hepatitis C is a boolean column; its curated mapping said `string`.

`build_writable_field_descriptor` prefers a mapping's `value_kind` over the
model column's type. The `hepatitis_c_status` row carried `string`, so the
negated `no_hepatitis_c_status` entry failed the resolver's boolean contract
and the row lost its editor, while the identical HIV and Hepatitis B rows kept
theirs. Correct the stale curation; the column itself is a BooleanField.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    mapping = apps.get_model('omop_core', 'FieldConceptMapping')
    mapping.objects.filter(
        field_name='hepatitis_c_status', value_kind='string',
    ).update(value_kind='boolean')


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0332_merge_20261109_2143'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
