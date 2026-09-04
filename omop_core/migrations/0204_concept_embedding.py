"""Add concept_embedding table for vector-similarity search.

Uses pgvector's vector(384) type and an IVFFlat index for cosine distance.
The table is ``managed = False`` in the Django model because Django's ORM has
no native pgvector field type -- we use raw SQL here and in queries.

The migration is a no-op on PostgreSQL instances where pgvector is not
installed (e.g. local dev with postgresql@14 via Homebrew). The suggest
pipeline gracefully falls through to lexical search when the table is absent.
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
