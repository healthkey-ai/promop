import django.contrib.postgres.indexes
import django.db.models.functions.text
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    # #262 step 1 of 2: build the functional trigram index on UPPER(concept_name)
    # under a NEW name, concurrently, WHILE the old raw-column
    # `ix_concept_name_trgm` is still in place. Django compiles
    # `concept_name__icontains` (concepts/search) to `UPPER(col::text) LIKE
    # UPPER(...)`, which the raw index cannot serve — the index expression must
    # match the query expression.
    #
    # Add (here) and drop (0129) are split into two recorded migrations, and the
    # add is made idempotent, so a partial failure is fully recoverable:
    #   - CREATE INDEX CONCURRENTLY can't run in a transaction (hence
    #     atomic=False) and is NOT idempotent — a cancelled/failed build leaves
    #     an INVALID index of the same name, and a bare retry would fail with
    #     "relation already exists". The leading DROP ... IF EXISTS clears any
    #     such leftover first (a no-op on the clean first run), so `migrate`
    #     retries cleanly without manual intervention.
    #   - Building the new index before dropping the old one means the old index
    #     keeps serving search throughout; a failed build never leaves the table
    #     unindexed, and 0129 removes the old index only once this migration is
    #     recorded as applied.
    atomic = False

    dependencies = [
        ('omop_core', '0127_patient_role_phase1'),
    ]

    operations = [
        migrations.RunSQL(
            sql='DROP INDEX CONCURRENTLY IF EXISTS ix_concept_name_upper_trgm',
            reverse_sql=migrations.RunSQL.noop,
        ),
        AddIndexConcurrently(
            model_name='concept',
            index=django.contrib.postgres.indexes.GinIndex(
                django.contrib.postgres.indexes.OpClass(
                    django.db.models.functions.text.Upper('concept_name'),
                    name='gin_trgm_ops',
                ),
                name='ix_concept_name_upper_trgm',
            ),
        ),
    ]
