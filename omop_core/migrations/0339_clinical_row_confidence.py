"""Provisional data, phase 0a (#2434, #2527): a nullable ``confidence`` on the
five clinical tables. Null is a real row; a number 0-100 is a provisional one.

Production data is unaffected until ``PROVISIONAL_DATA_ENABLED`` is turned on,
which must wait for the derivation filter (#2529): nothing yet keeps a
provisional row out of ``patient_record``.

Written to be safe on the large tables (``measurement`` holds the bulk of an
instance) and to be re-run after a partial failure, because ``atomic = False``
means nothing rolls back and Django records the migration only at the end:

* ``ADD COLUMN IF NOT EXISTS`` with no default is a catalogue change. The column
  is a plain ``smallint``: ``PositiveSmallIntegerField`` would add a ``>= 0``
  check to the same statement, and a check on an added column scans the table
  under an ACCESS EXCLUSIVE lock.
* The range check is added ``NOT VALID`` (guarded, so a retry skips it) and
  validated separately. VALIDATE takes only SHARE UPDATE EXCLUSIVE, so writes
  continue during the scan.
* The short ``lock_timeout`` around the two statements that need an exclusive
  lock makes them fail fast, instead of queueing every other query on the table
  behind a long transaction. It is reset before the index builds, which wait on
  other transactions by design.
* The partial indexes are built ``CONCURRENTLY``. An interrupted build leaves an
  INVALID index that ``IF NOT EXISTS`` would accept, so a retry drops the index
  first.
"""

import django.core.validators
from django.contrib.postgres.operations import AddIndexConcurrently  # noqa: F401
from django.db import migrations, models

# model name -> (table, short name used in the index and constraint names)
TABLES = {
    "conditionoccurrence": ("condition_occurrence", "cond"),
    "drugexposure": ("drug_exposure", "de"),
    "procedureoccurrence": ("procedure_occurrence", "proc"),
    "measurement": ("measurement", "meas"),
    "observation": ("observation", "obs"),
}


def _field():
    return models.SmallIntegerField(
        blank=True,
        help_text="Null for a real row; the confidence percent of the proposed mapping for a provisional one",
        null=True,
        validators=[
            django.core.validators.MinValueValidator(0),
            django.core.validators.MaxValueValidator(100),
        ],
    )


def _constraint(short):
    return models.CheckConstraint(
        condition=models.Q(("confidence__gte", 0), ("confidence__lte", 100)),
        name=f"ck_{short}_confidence_pct",
    )


def _index(short):
    return models.Index(
        fields=["person"],
        condition=models.Q(("confidence__isnull", False)),
        name=f"ix_{short}_provisional",
    )


def _add_column(model, table):
    return migrations.RunSQL(
        sql=f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "confidence" smallint NULL',
        reverse_sql=f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "confidence"',
        state_operations=[migrations.AddField(model_name=model, name="confidence", field=_field())],
    )


def _add_constraint(model, table, short):
    name = f"ck_{short}_confidence_pct"
    return migrations.RunSQL(
        sql=(
            "DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = '{name}' "
            f"AND conrelid = '{table}'::regclass) THEN "
            f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
            "CHECK (confidence >= 0 AND confidence <= 100) NOT VALID; "
            "END IF; END $$"
        ),
        reverse_sql=f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"',
        # Django's state learns the constraint here; the SQL above is what
        # creates it, so the state operation must not run any.
        state_operations=[migrations.AddConstraint(model_name=model, constraint=_constraint(short))],
    )


def _validate(table, short):
    return migrations.RunSQL(
        sql=f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "ck_{short}_confidence_pct"',
        reverse_sql=migrations.RunSQL.noop,
    )


def _build_index(model, table, short):
    name = f"ix_{short}_provisional"
    return migrations.RunSQL(
        sql=[
            f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"',
            f'CREATE INDEX CONCURRENTLY "{name}" ON "{table}" ("person_id") '
            "WHERE confidence IS NOT NULL",
        ],
        reverse_sql=f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"',
        state_operations=[migrations.AddIndex(model_name=model, index=_index(short))],
    )


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("omop_core", "0338_source_record_key_unique"),
    ]

    operations = (
        [migrations.RunSQL("SET lock_timeout = '10s'", reverse_sql="SET lock_timeout = '10s'")]
        + [_add_column(m, table) for m, (table, _s) in TABLES.items()]
        + [_add_constraint(m, table, short) for m, (table, short) in TABLES.items()]
        + [migrations.RunSQL("RESET lock_timeout", reverse_sql="RESET lock_timeout")]
        + [_validate(table, short) for m, (table, short) in TABLES.items()]
        + [_build_index(m, table, short) for m, (table, short) in TABLES.items()]
    )
