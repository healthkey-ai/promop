"""Populate the concept_embedding table for vector-similarity search.

Embeds concept_name using BAAI/bge-small-en-v1.5 (384 dimensions) and stores
the vectors in pgvector.  After the table is populated it (re)creates the
IVFFlat index.

Usage:
    manage.py build_concept_embeddings                     # all standard concepts
    manage.py build_concept_embeddings --vocabulary-id SNOMED  # one vocabulary
    manage.py build_concept_embeddings --batch-size 1024   # larger batches
    manage.py build_concept_embeddings --force              # re-embed existing
"""
import logging
import time

from django.core.management.base import BaseCommand
from django.db import connection

from omop_core.models import Concept

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 512
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

        # Build queryset of concepts to embed.
        qs = Concept.objects.filter(
            standard_concept='S',
            invalid_reason__isnull=True,
        ).order_by('concept_id')

        if vocab_id:
            qs = qs.filter(vocabulary_id=vocab_id)
            self.stdout.write(f'Filtering to vocabulary_id={vocab_id}')

        if not force:
            # Exclude concepts that already have embeddings.
            with connection.cursor() as cur:
                cur.execute('SELECT concept_id FROM concept_embedding')
                existing = {row[0] for row in cur.fetchall()}
            if existing:
                qs = qs.exclude(concept_id__in=existing)
                self.stdout.write(f'Skipping {len(existing)} already-embedded concepts.')

        total = qs.count()
        self.stdout.write(f'Embedding {total} concepts in batches of {batch_size}...')

        t0 = time.time()
        processed = 0
        batch_ids = []
        batch_names = []

        for concept in qs.iterator(chunk_size=batch_size):
            batch_ids.append(concept.concept_id)
            batch_names.append(concept.concept_name)

            if len(batch_ids) >= batch_size:
                processed += self._flush_batch(model, batch_ids, batch_names)
                batch_ids.clear()
                batch_names.clear()
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                self.stdout.write(
                    f'  {processed}/{total} ({rate:.0f} concepts/s)',
                    ending='\r',
                )

        # Final partial batch.
        if batch_ids:
            processed += self._flush_batch(model, batch_ids, batch_names)

        elapsed = time.time() - t0
        self.stdout.write(f'\nEmbedded {processed} concepts in {elapsed:.1f}s.')

        # Rebuild the IVFFlat index now that rows exist.
        self.stdout.write('Rebuilding IVFFlat index...')
        with connection.cursor() as cur:
            cur.execute('DROP INDEX IF EXISTS ix_concept_embedding_cosine')
            cur.execute("""
                CREATE INDEX ix_concept_embedding_cosine
                    ON concept_embedding
                    USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 1000)
            """)
        self.stdout.write('Done.')

    def _flush_batch(self, model, ids, names):
        """Encode and upsert one batch. Returns count of rows written."""
        vectors = model.encode(names, show_progress_bar=False)
        with connection.cursor() as cur:
            args = [
                (cid, vec.tolist())
                for cid, vec in zip(ids, vectors)
            ]
            cur.executemany(
                """
                INSERT INTO concept_embedding (concept_id, embedding)
                VALUES (%s, %s::vector)
                ON CONFLICT (concept_id)
                    DO UPDATE SET embedding = EXCLUDED.embedding
                """,
                args,
            )
        return len(ids)
