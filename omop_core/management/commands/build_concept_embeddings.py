"""Populate the concept_embedding table for vector-similarity search.

Embeds concept_name using BAAI/bge-small-en-v1.5 (384 dimensions) and stores
the vectors in pgvector.  After the table is populated it (re)creates the
IVFFlat index.

Designed for remote databases (Render): writes in small batches (64 rows),
auto-reconnects on connection drops, and skips concepts already embedded so
a re-run picks up where the last one left off.

Usage:
    manage.py build_concept_embeddings                     # all standard concepts
    manage.py build_concept_embeddings --vocabulary-id SNOMED  # one vocabulary
    manage.py build_concept_embeddings --batch-size 1024   # larger encode batches
    manage.py build_concept_embeddings --force              # re-embed existing
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import connection

from omop_core.models import Concept

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 512
# DB write batch is deliberately smaller than encode batch — each row is 384
# floats (~3KB as text), so 64 rows is ~200KB per INSERT, well within Render's
# statement limits.
DB_WRITE_BATCH = 64
MODEL_NAME = 'BAAI/bge-small-en-v1.5'
EMBEDDING_DIM = 384


class Command(BaseCommand):
    help = 'Build sentence-transformer embeddings for standard OMOP concepts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--batch-size', type=int, default=DEFAULT_BATCH_SIZE,
            help=f'Rows per embedding batch (default {DEFAULT_BATCH_SIZE}).',
        )
        parser.add_argument(
            '--vocabulary-id', type=str, default=None,
            help='Limit to one vocabulary (e.g. SNOMED, LOINC).',
        )
        parser.add_argument(
            '--force', action='store_true',
            help='Re-embed concepts that already have an embedding.',
        )

    def handle(self, **options):
        batch_size = options['batch_size']
        vocab_id = options['vocabulary_id']
        force = options['force']

        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            self.stderr.write(
                'sentence-transformers is not installed. '
                'Run: pip install sentence-transformers'
            )
            return

        self.stdout.write(f'Loading model {MODEL_NAME}...')
        model = SentenceTransformer(MODEL_NAME)

        # Load concept IDs + names into memory so we don't hold a server-side
        # cursor open for the entire run.  ~1.5M rows × ~60 bytes ≈ 90MB.
        self.stdout.write('Loading concept list...')
        qs = Concept.objects.filter(
            standard_concept='S',
            invalid_reason__isnull=True,
        ).order_by('concept_id')

        if vocab_id:
            qs = qs.filter(vocabulary_id=vocab_id)
            self.stdout.write(f'Filtering to vocabulary_id={vocab_id}')

        all_concepts = list(qs.values_list('concept_id', 'concept_name'))
        self.stdout.write(f'Loaded {len(all_concepts)} concepts.')

        if not force:
            existing = self._existing_ids()
            before = len(all_concepts)
            all_concepts = [(cid, name) for cid, name in all_concepts if cid not in existing]
            skipped = before - len(all_concepts)
            if skipped:
                self.stdout.write(f'Skipping {skipped} already-embedded concepts.')

        total = len(all_concepts)
        if total == 0:
            self.stdout.write('Nothing to embed.')
            return

        self.stdout.write(f'Embedding {total} concepts in batches of {batch_size}...')

        t0 = time.time()
        processed = 0

        for i in range(0, total, batch_size):
            chunk = all_concepts[i:i + batch_size]
            ids = [c[0] for c in chunk]
            names = [c[1] for c in chunk]

            vectors = model.encode(names, show_progress_bar=False)

            # Write in smaller sub-batches to stay within statement limits.
            pairs = list(zip(ids, vectors))
            for j in range(0, len(pairs), DB_WRITE_BATCH):
                sub = pairs[j:j + DB_WRITE_BATCH]
                self._write_batch(sub)

            processed += len(chunk)
            elapsed = time.time() - t0
            rate = processed / elapsed if elapsed > 0 else 0
            self.stdout.write(
                f'  {processed}/{total} ({rate:.0f} concepts/s)',
                ending='\r',
            )

        elapsed = time.time() - t0
        self.stdout.write(f'\nEmbedded {processed} concepts in {elapsed:.1f}s.')

        # Rebuild the IVFFlat index now that rows exist.
        self.stdout.write('Rebuilding IVFFlat index...')
        self._ensure_connection()
        with connection.cursor() as cur:
            cur.execute('DROP INDEX IF EXISTS ix_concept_embedding_cosine')
            cur.execute("""
                CREATE INDEX ix_concept_embedding_cosine
                    ON concept_embedding
                    USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 1000)
            """)
        self.stdout.write('Done.')

    def _existing_ids(self):
        """Set of concept_ids that already have embeddings."""
        self._ensure_connection()
        with connection.cursor() as cur:
            cur.execute('SELECT concept_id FROM concept_embedding')
            return {row[0] for row in cur.fetchall()}

    def _write_batch(self, pairs, retries=3):
        """Upsert a small batch of (concept_id, vector) pairs with retry."""
        args = [(cid, vec.tolist()) for cid, vec in pairs]
        for attempt in range(retries):
            try:
                self._ensure_connection()
                with connection.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO concept_embedding (concept_id, embedding)
                        VALUES (%s, %s::vector)
                        ON CONFLICT (concept_id)
                            DO UPDATE SET embedding = EXCLUDED.embedding
                        """,
                        args,
                    )
                return
            except Exception as exc:
                if attempt < retries - 1:
                    logger.warning(
                        'DB write failed (attempt %d/%d): %s — reconnecting',
                        attempt + 1, retries, exc,
                    )
                    connection.close()
                    time.sleep(2 ** attempt)
                else:
                    raise

    def _ensure_connection(self):
        """Reopen the connection if it was dropped."""
        connection.ensure_connection()
