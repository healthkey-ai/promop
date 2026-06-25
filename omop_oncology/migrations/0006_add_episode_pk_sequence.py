"""Create PostgreSQL sequence for Episode table (manual BigIntegerField PK).

The episode table was missing from omop_core's 0074_add_pk_sequences migration
because it lives in a different app. next_pk() requires a sequence.
"""
from django.db import migrations


def create_episode_sequence(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    cursor.execute('CREATE SEQUENCE IF NOT EXISTS "episode_episode_id_seq"')
    cursor.execute(
        'SELECT setval(%s, COALESCE(MAX("episode_id"), 0) + 1, false) FROM "episode"',
        ['episode_episode_id_seq'],
    )


def drop_episode_sequence(apps, schema_editor):
    cursor = schema_editor.connection.cursor()
    cursor.execute('DROP SEQUENCE IF EXISTS "episode_episode_id_seq"')


class Migration(migrations.Migration):
    dependencies = [
        ('omop_oncology', '0005_rename_ai_lot_episode_idx_ai_line_of__episode_b79358_idx_and_more'),
    ]

    operations = [
        migrations.RunPython(create_episode_sequence, drop_episode_sequence),
    ]
