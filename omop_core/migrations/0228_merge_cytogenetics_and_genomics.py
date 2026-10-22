"""Join the #1204 genomics branch and enable lossless summary NOTE storage."""

from importlib import import_module

from django.db import migrations


def repair_pending_edits(apps, schema_editor):
    migration = import_module('omop_core.migrations.0222_cytogenetic_markers')
    migration.rename_pending_edits(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ('omop_core', '0225_merge_cytogenetic_markers'),
        ('omop_core', '0227_seed_genomics_v2_components'),
    ]

    operations = [
        # Also repair databases that applied the earlier development rename.
        migrations.RunPython(repair_pending_edits, migrations.RunPython.noop),
        # Both genomics and cytogenetic summaries allocate NOTE IDs using
        # next_pk. pytest's fixture created this sequence, but migrations did
        # not. next_pk advances it past existing IDs before each allocation.
        # Keep it on rollback: the genomics branch also uses the sequence.
        migrations.RunSQL(
            'CREATE SEQUENCE IF NOT EXISTS note_note_id_seq',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
