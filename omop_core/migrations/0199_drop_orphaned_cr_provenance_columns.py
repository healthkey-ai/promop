"""Drop orphaned provenance columns from concept_relationship.

Migrations 0196-0198 originally added provenance columns to CR.  Those
migrations are now no-ops, but any database that applied the originals
still has the columns.  This migration conditionally drops them so the
DB schema matches the model.
"""
from django.db import migrations

_ORPHANED_COLUMNS = [
    'source',
    'origin_system',
    'status',
    'notes',
    'reviewer_id',
    'reviewed_at',
    'updated_at',
]


def drop_orphaned_columns(apps, schema_editor):
    with schema_editor.connection.cursor() as cur:
        for col in _ORPHANED_COLUMNS:
            cur.execute(
                'ALTER TABLE concept_relationship '
                'DROP COLUMN IF EXISTS %s' % col
            )


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0198_add_index_to_cr_source'),
    ]

    operations = [
        migrations.RunPython(drop_orphaned_columns, migrations.RunPython.noop),
    ]
