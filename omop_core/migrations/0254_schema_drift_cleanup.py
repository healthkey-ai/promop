"""Clean up schema drift on databases where replaced migrations left orphan objects.

Three items exist on the Render staging database but have no corresponding
Django model or migration in the current codebase:

1. ``release_table_change`` table — created by the Athena loader's
   ``publish_release()`` raw SQL and a since-removed ``ReleaseTableChange``
   model.  The loader no longer writes to it.  Dropped here.

2. ``organization.public_data`` column — created by an unknown migration or
   branch that was subsequently removed.  Not on the ``Organization`` model.
   Dropped here.

3. ``uq_stcm_natural_key`` unique constraint on ``source_to_concept_map`` —
   created by the original ``0122_sourcetoconceptmap_uq_stcm_natural_key``
   migration, which was replaced by ``0122_add_clinical_list_indexes_…``.
   The constraint is useful for the Athena loader's natural-key dedup, so it
   is **re-added to the model** (see models.py) and this migration creates it
   on databases that don't have it yet.  Databases that already have it (staging)
   skip the CREATE via IF NOT EXISTS.

Every statement uses IF EXISTS / IF NOT EXISTS so the migration is safe on
databases that never had the drift (fresh installs, CI).
"""

from django.db import migrations, models


def drop_orphan_objects(apps, schema_editor):
    """Drop tables/columns that no current model owns."""
    conn = schema_editor.connection
    with conn.cursor() as cur:
        cur.execute('DROP TABLE IF EXISTS release_table_change CASCADE')
        # organization.public_data — check before ALTER to avoid error on fresh DBs
        cur.execute("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'organization' AND column_name = 'public_data'
        """)
        if cur.fetchone():
            cur.execute('ALTER TABLE organization DROP COLUMN public_data')


def add_stcm_constraint(apps, schema_editor):
    """Add uq_stcm_natural_key if it does not already exist."""
    conn = schema_editor.connection
    with conn.cursor() as cur:
        cur.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_stcm_natural_key
            ON source_to_concept_map (
                source_code, source_vocabulary_id, target_concept_id,
                valid_start_date
            )
        """)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0253_reconcile_athena_icd10_mappings'),
    ]

    operations = [
        # 1. Drop orphan table and column
        migrations.RunPython(drop_orphan_objects, noop),

        # 2. Add the natural-key constraint to SourceToConceptMap.
        #    SeparateDatabaseAndState: the RunPython uses IF NOT EXISTS so it
        #    is safe on staging (already has the index) and fresh installs
        #    (creates it).  The state_operations tell Django the model now has
        #    the constraint so makemigrations --check stays clean.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(add_stcm_constraint, noop),
            ],
            state_operations=[
                migrations.AddConstraint(
                    model_name='sourcetoconceptmap',
                    constraint=models.UniqueConstraint(
                        fields=(
                            'source_code',
                            'source_vocabulary_id',
                            'target_concept',
                            'valid_start_date',
                        ),
                        name='uq_stcm_natural_key',
                    ),
                ),
            ],
        ),
    ]
