"""Transition ConceptEmbedding to managed=True with VectorField.

Django's model state moves in two steps:

1. AlterModelOptions removes managed=False so Django owns the schema going
   forward (pgvector is available on every first-class target database).
2. AlterField changes the placeholder BinaryField to pgvector's VectorField so
   Django tooling knows the real column type.

Both are **state-only**, wrapped in SeparateDatabaseAndState with no database
operations.  The table this migration describes is not created by Django: 0204
creates ``concept_embedding`` with raw SQL, already as ``vector(384)``, and
*skips* it on a server that does not ship pgvector.  On such a server there is
no column to alter, and the unconditional ``ALTER TABLE`` that a declarative
AlterField emits failed with ``relation "concept_embedding" does not exist`` —
so ``migrate`` could not build a database from empty on a stock PostgreSQL
image at all (#1430).  Deployments created before this migration never
noticed, because their table already existed.

There is no DDL to emit even where the table does exist: 0204 is the only
creator and 0205's unmanaged CreateModel produces none, so every reachable
column is ``vector(384)`` already.  A RunPython alternative was rejected
because ``sqlmigrate`` executes RunPython inside SeparateDatabaseAndState for
real, against the target database.

Invariant for later migrations: after this one the model state says the table
is managed, but on a pgvector-less database it does not exist.  Any future
operation on ConceptEmbedding (AddIndex, AddField, AlterField ...) must be
state-only or guard on the table's existence, or it reintroduces #1430.
CI runs on a pgvector image, so it will not catch the regression.
"""
import pgvector.django.vector
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("omop_core", "0205_add_suggest_strategy_to_sccm"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterModelOptions(
                    name="conceptembedding",
                    options={},
                ),
                migrations.AlterField(
                    model_name="conceptembedding",
                    name="embedding",
                    field=pgvector.django.vector.VectorField(dimensions=384),
                ),
            ],
            database_operations=[],
        ),
    ]
