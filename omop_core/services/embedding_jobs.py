"""Run candidate embedding maintenance after vocabulary and mapping loads."""
import logging

from django.conf import settings
from django.core.management import call_command
from django.db import connection, transaction

logger = logging.getLogger(__name__)


def run_suggest_embeddings():
    from omop_core.mapping.suggestions import LEXICAL_LIMIT_MAX

    call_command('precompute_suggest_embeddings', min_occurrences=1,
                 lexical_limit=LEXICAL_LIMIT_MAX)


def concept_embedding_table_exists():
    """Whether this database has somewhere to put embeddings.

    Migration 0204 skips ``concept_embedding`` on a server without pgvector
    (#1430), and nothing creates it later, so every loader would otherwise
    finish by crashing its precompute — inline, after the load had already
    committed.
    """
    return 'concept_embedding' in connection.introspection.table_names()


def dispatch_suggest_embeddings():
    """Use Celery when configured, and inline execution otherwise, after commit."""
    def run():
        if not concept_embedding_table_exists():
            logger.warning(
                'concept_embedding does not exist (pgvector unavailable when '
                'migrations ran); skipping suggest embedding maintenance.'
            )
            return
        if getattr(settings, 'CELERY_BROKER_URL', ''):
            from omop_core.tasks import precompute_suggest_embeddings_task

            precompute_suggest_embeddings_task.delay()
        else:
            run_suggest_embeddings()

    transaction.on_commit(run)
