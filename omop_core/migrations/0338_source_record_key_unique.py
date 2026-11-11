from django.db import migrations, models


def _unique(table):
    """A partial unique index on (person_id, source_record_key) for keyed rows,
    built without blocking writes.

    Partial, so the index holds only keyed rows: none today, instead of every
    row of the table. The keyed upsert inserts with ON CONFLICT DO NOTHING and
    no conflict target, which a partial unique index satisfies.

    CREATE INDEX CONCURRENTLY cannot run in a transaction, and a failed build
    leaves an INVALID index of the same name, so each build is preceded by
    DROP ... IF EXISTS. A retry rebuilds both; the build is a scan that writes
    almost nothing.
    """
    name = f'uq_{table}_person_source_record_key'
    return migrations.SeparateDatabaseAndState(
        database_operations=[
            migrations.RunSQL(
                sql=f'DROP INDEX CONCURRENTLY IF EXISTS {name}',
                reverse_sql=migrations.RunSQL.noop,
            ),
            migrations.RunSQL(
                sql=(
                    f'CREATE UNIQUE INDEX CONCURRENTLY {name} '
                    f'ON {table} (person_id, source_record_key) '
                    f'WHERE source_record_key IS NOT NULL'
                ),
                reverse_sql=f'DROP INDEX CONCURRENTLY IF EXISTS {name}',
            ),
        ],
        state_operations=[
            migrations.AddConstraint(
                model_name=table,
                constraint=models.UniqueConstraint(
                    fields=('person', 'source_record_key'),
                    condition=models.Q(source_record_key__isnull=False),
                    name=name,
                ),
            ),
        ],
    )


class Migration(migrations.Migration):
    # #2453: a plain CREATE UNIQUE INDEX on measurement (tens of millions of
    # rows) would block its writes for the whole build.
    atomic = False

    dependencies = [
        ('omop_core', '0337_source_record_key'),
    ]

    operations = [_unique('measurement'), _unique('observation')]
