"""Provisional data, phase 0a (#2434, #2527): a nullable ``confidence`` on the
five clinical tables. Null is a real row; a number 0-100 is a provisional one.

Nothing reads or writes the column yet, so this changes no behaviour.

Written to be safe on the large tables (``measurement`` holds the bulk of an
instance):

* ``ADD COLUMN`` with no default is a catalogue change. The column is a plain
  ``smallint``: ``PositiveSmallIntegerField`` would add a ``>= 0`` check to the
  same statement, and a check on an added column scans the table under an
  ACCESS EXCLUSIVE lock.
* The range check is added ``NOT VALID`` and validated separately. VALIDATE
  takes only SHARE UPDATE EXCLUSIVE, so writes continue during the scan.
* The partial indexes are built ``CONCURRENTLY``, hence ``atomic = False``,
  because a plain build blocks writes for as long as the table scan takes.
"""

import django.core.validators
from django.contrib.postgres.operations import AddIndexConcurrently
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


def _constraint_ops(model, table, short):
    name = f"ck_{short}_confidence_pct"
    return [
        migrations.RunSQL(
            sql=(f'ALTER TABLE "{table}" ADD CONSTRAINT "{name}" '
                 f'CHECK (confidence >= 0 AND confidence <= 100) NOT VALID'),
            reverse_sql=f'ALTER TABLE "{table}" DROP CONSTRAINT IF EXISTS "{name}"',
            # Django's state learns the constraint here; the SQL above is what
            # creates it, so the state operation must not run any.
            state_operations=[migrations.AddConstraint(model_name=model, constraint=_constraint(short))],
        ),
        migrations.RunSQL(
            sql=f'ALTER TABLE "{table}" VALIDATE CONSTRAINT "{name}"',
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("omop_core", "0338_source_record_key_unique"),
    ]

    operations = (
        [migrations.AddField(model_name=m, name="confidence", field=_field()) for m in TABLES]
        + [op for m, (table, short) in TABLES.items() for op in _constraint_ops(m, table, short)]
        + [
            AddIndexConcurrently(
                model_name=m,
                index=models.Index(
                    fields=["person"],
                    condition=models.Q(("confidence__isnull", False)),
                    name=f"ix_{short}_provisional",
                ),
            )
            for m, (table, short) in TABLES.items()
        ]
    )
