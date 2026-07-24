from django.contrib.postgres.operations import RemoveIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    # #262 step 2 of 2: drop the old raw-column `ix_concept_name_trgm` now that
    # the functional `ix_concept_name_upper_trgm` (migration 0121) is built and
    # serving `concepts/search`. DROP INDEX CONCURRENTLY avoids the ACCESS
    # EXCLUSIVE lock on the (large) concept table and can't run in a transaction,
    # so this migration is non-atomic. Because 0121 is recorded before this runs,
    # a failure here leaves the new index in place — search stays indexed and the
    # drop simply retries.
    atomic = False

    dependencies = [
        ('omop_core', '0121_concept_ix_concept_name_upper_trgm'),
    ]

    operations = [
        RemoveIndexConcurrently(
            model_name='concept',
            name='ix_concept_name_trgm',
        ),
    ]
