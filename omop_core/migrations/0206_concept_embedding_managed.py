"""Transition ConceptEmbedding to managed=True with VectorField.

Django's model state moves in two steps:

1. AlterModelOptions removes managed=False so Django owns the schema going
   forward (pgvector is available on every first-class target database).
2. AlterField changes the placeholder BinaryField to pgvector's VectorField so
   Django tooling knows the real column type.

Both are state-only here.  The matching DDL runs from ``align_embedding_column``
instead, because the table this migration alters is not created by Django:
0204 creates ``concept_embedding`` with raw SQL, and *skips* it on a server that
does not ship pgvector.  On such a server there is no column to alter, and the
unconditional ``ALTER TABLE`` that AlterField used to emit failed with
``relation "concept_embedding" does not exist`` — so ``migrate`` could not build
a database from empty on a stock PostgreSQL image at all (#1430).  Deployments
created before this migration never noticed, because their table already
existed.

Where the table does exist the emitted SQL is unchanged from the declarative
AlterField, so already-migrated databases are unaffected.
"""
import logging

import pgvector.django.vector
from django.db import migrations

logger = logging.getLogger(__name__)


def align_embedding_column(apps, schema_editor):
    """Give ``concept_embedding.embedding`` the real vector(384) column type.

    A no-op in practice on databases whose table came from 0204 (it already
    creates the column as ``vector(384)``); it exists so a table created
    elsewhere ends up matching the model.  When 0204 skipped the table there is
    nothing to align, and the model state still advances so that a later
    ``makemigrations`` does not see a phantom change.
    """
    connection = schema_editor.connection
    if 'concept_embedding' not in connection.introspection.table_names():
        logger.warning(
            'concept_embedding does not exist — migration 0204 skipped it '
            'because the pgvector extension is not available on this server. '
            'Leaving it uncreated; vector reranking in code-mapping suggest '
            'stays unavailable until pgvector is installed.'
        )
        return
    with connection.cursor() as cur:
        cur.execute(
            'ALTER TABLE "concept_embedding" ALTER COLUMN "embedding" '
            'TYPE vector(384) USING "embedding"::vector(384)'
        )


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
            database_operations=[
                migrations.RunPython(
                    align_embedding_column,
                    migrations.RunPython.noop,
                ),
            ],
        ),
    ]
