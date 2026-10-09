"""Add concept_embedding table for vector-similarity search.

Uses pgvector's vector(384) type and an IVFFlat index for cosine distance.
Migration 0206 transitions the model to managed=True with a proper VectorField;
this migration uses raw SQL because the model was initially managed=False.

The RunPython still checks pg_available_extensions defensively, but pgvector is
expected on all target databases (Render staging/production and local dev on
PostgreSQL 18).
"""
import logging

from django.db import connection, migrations

logger = logging.getLogger(__name__)


def create_vector_table(apps, schema_editor):
    """Create pgvector extension + concept_embedding table if pgvector is available."""
    with connection.cursor() as cur:
        # Check whether the vector extension is available on this server.
        cur.execute(
            "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
        )
        if not cur.fetchone():
            logger.info(
                'pgvector extension not available on this server — '
                'skipping concept_embedding table creation.'
            )
            return

        cur.execute('CREATE EXTENSION IF NOT EXISTS vector')
        cur.execute("""\
            CREATE TABLE IF NOT EXISTS concept_embedding (
                concept_id  INTEGER PRIMARY KEY
                    REFERENCES concept(concept_id) ON DELETE CASCADE,
                embedding   vector(384) NOT NULL
            )
        """)
        cur.execute("""\
            CREATE INDEX IF NOT EXISTS ix_concept_embedding_cosine
                ON concept_embedding
                USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 1000)
        """)


def drop_vector_table(apps, schema_editor):
    """Reverse: drop the table and index (extension left in place)."""
    with connection.cursor() as cur:
        cur.execute('DROP INDEX IF EXISTS ix_concept_embedding_cosine')
        cur.execute('DROP TABLE IF EXISTS concept_embedding')


class Migration(migrations.Migration):

    dependencies = [
        ('omop_core', '0203_raw_umls_tables'),
    ]

    operations = [
        migrations.RunPython(create_vector_table, drop_vector_table),
    ]
