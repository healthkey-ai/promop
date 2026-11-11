from django.db import migrations, models


def _unique(table):
    """Build the (person_id, source_record_key) unique constraint without
    blocking writes, on the pattern of 0144.

    A plain unique constraint, not a partial index: NULLs are distinct, so the
    unkeyed rows never collide, and ON CONFLICT (person_id, source_record_key)
    finds it without a predicate.

    CREATE INDEX CONCURRENTLY cannot run in a transaction, and a failed build
    leaves an INVALID index of the same name. Every step is therefore safe to
    repeat: a retry drops what a stopped run left and builds it again.
    """
    name = f'uq_{table}_person_source_record_key'
    tmp = f'ix_{table}_source_record_key_build'
    return migrations.SeparateDatabaseAndState(
        database_operations=[
            migrations.RunSQL(
                sql=f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}',
                reverse_sql=migrations.RunSQL.noop,
            ),
            migrations.RunSQL(
                sql=f'DROP INDEX CONCURRENTLY IF EXISTS {tmp}',
                reverse_sql=migrations.RunSQL.noop,
            ),
            migrations.RunSQL(
                sql=(
                    f'CREATE UNIQUE INDEX CONCURRENTLY {tmp} '
                    f'ON {table} (person_id, source_record_key)'
                ),
                reverse_sql=migrations.RunSQL.noop,
            ),
            # Attaching a valid index takes a brief lock and no scan; the
            # index is renamed to the constraint's name.
            migrations.RunSQL(
                sql=(
                    f'ALTER TABLE {table} ADD CONSTRAINT {name} '
                    f'UNIQUE USING INDEX {tmp}'
                ),
                reverse_sql=f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}',
            ),
        ],
        state_operations=[
            migrations.AddConstraint(
                model_name=table,
                constraint=models.UniqueConstraint(
                    fields=('person', 'source_record_key'), name=name,
                ),
            ),
        ],
    )


class Migration(migrations.Migration):
    # #2453: measurement holds tens of millions of rows; a plain unique index
    # build would block its writes for the whole deploy.
    atomic = False

    dependencies = [
        ('omop_core', '0337_source_record_key'),
    ]

    operations = [_unique('measurement'), _unique('observation')]
