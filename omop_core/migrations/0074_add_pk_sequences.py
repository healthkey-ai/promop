"""Create PostgreSQL sequences for OMOP tables that use manual integer PKs.

Each sequence is seeded from the current max value in the table so existing
rows are not affected.
"""
from django.db import migrations

_SEQUENCES = [
    ('measurement', 'measurement_id'),
    ('visit_occurrence', 'visit_occurrence_id'),
    ('care_site', 'care_site_id'),
    ('concept', 'concept_id'),
    ('condition_occurrence', 'condition_occurrence_id'),
    ('drug_exposure', 'drug_exposure_id'),
    ('observation', 'observation_id'),
    ('procedure_occurrence', 'procedure_occurrence_id'),
    ('person', 'person_id'),
]


def create_sequences(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for table, pk_field in _SEQUENCES:
        seq_name = f'{table}_{pk_field}_seq'
        cursor.execute(f'CREATE SEQUENCE IF NOT EXISTS "{seq_name}"')
        cursor.execute(
            f'SELECT setval(%s, COALESCE(MAX("{pk_field}"), 0) + 1, false) '
            f'FROM "{table}"',
            [seq_name],
        )


def drop_sequences(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    for table, pk_field in _SEQUENCES:
        seq_name = f'{table}_{pk_field}_seq'
        cursor.execute(f'DROP SEQUENCE IF EXISTS "{seq_name}"')


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0073_add_verification_status_and_expires_at'),
    ]

    operations = [
        migrations.RunPython(create_sequences, drop_sequences),
    ]
