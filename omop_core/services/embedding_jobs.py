"""Run candidate embedding maintenance after vocabulary and mapping loads."""
from django.conf import settings
from django.core.management import call_command
from django.db import transaction


def run_suggest_embeddings():
    from omop_core.mapping.suggestions import LEXICAL_LIMIT_MAX

    call_command('precompute_suggest_embeddings', min_occurrences=1,
                 lexical_limit=LEXICAL_LIMIT_MAX)


def dispatch_suggest_embeddings():
    """Use Celery when configured, and inline execution otherwise, after commit."""
    def run():
        if getattr(settings, 'CELERY_BROKER_URL', ''):
            from omop_core.tasks import precompute_suggest_embeddings_task

            precompute_suggest_embeddings_task.delay()
        else:
            run_suggest_embeddings()

    transaction.on_commit(run)
