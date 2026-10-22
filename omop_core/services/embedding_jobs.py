"""Build concept embeddings after vocabulary and mapping loads."""
import logging

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction

logger = logging.getLogger(__name__)


def run_build_concept_embeddings():
    """Run the full concept embedding build (resumable — skips existing)."""
    call_command('build_concept_embeddings')


def concept_embedding_table_exists():
    """Whether this database has somewhere to put embeddings.

    Migration 0204 skips ``concept_embedding`` on a server without pgvector
    (#1430), and nothing creates it later, so every loader would otherwise
    finish by crashing its precompute — inline, after the load had already
    committed.
    """
    return 'concept_embedding' in connection.introspection.table_names()


def dispatch_concept_embedding_build():
    """Queue a full concept embedding build on Celery, or run inline."""
    def run():
        if not concept_embedding_table_exists():
            logger.warning(
                'concept_embedding does not exist (pgvector unavailable when '
                'migrations ran); skipping concept embedding build.'
            )
            return
        if getattr(settings, 'CELERY_BROKER_URL', ''):
            from omop_core.tasks import build_concept_embeddings_task

            build_concept_embeddings_task.delay()
        else:
            run_build_concept_embeddings()

    transaction.on_commit(run)


# Keep the old name as an alias — callers and tests may still reference it.
dispatch_suggest_embeddings = dispatch_concept_embedding_build
