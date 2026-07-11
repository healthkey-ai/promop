"""Create PostgreSQL sequences for Location and VisitDetail tables.

These tables use manual BigIntegerField PKs but were omitted from the
0074_add_pk_sequences migration. next_pk() requires the sequences to exist.
"""
from django.db import migrations

_MISSING_SEQUENCES = [
    ('location', 'location_id'),
    ('visit_detail', 'visit_detail_id'),
]


def create_sequences(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for table, pk_field in _MISSING_SEQUENCES:
        seq_name = f'{table}_{pk_field}_seq'
        cursor.execute(f'CREATE SEQUENCE IF NOT EXISTS "{seq_name}"')
        cursor.execute(
            f'SELECT setval(%s, COALESCE(MAX("{pk_field}"), 0) + 1, false) '
            f'FROM "{table}"',
            [seq_name],
        )


def drop_sequences(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for table, pk_field in _MISSING_SEQUENCES:
        seq_name = f'{table}_{pk_field}_seq'
        cursor.execute(f'DROP SEQUENCE IF EXISTS "{seq_name}"')


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0107_add_omop_tables'),
    ]

    operations = [
        migrations.RunPython(create_sequences, drop_sequences),
    ]
