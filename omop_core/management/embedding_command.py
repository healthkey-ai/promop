"""Shared completion hook for vocabulary and SCCM loading commands."""
from contextvars import ContextVar

from django.core.management.base import BaseCommand

from omop_core.services.embedding_jobs import dispatch_suggest_embeddings


_inside_load = ContextVar('inside_embedding_load', default=False)


class EmbeddingLoadCommand(BaseCommand):
    def create_parser(self, prog_name, subcommand, **kwargs):
        parser = super().create_parser(prog_name, subcommand, **kwargs)
        parser.add_argument(
            '--skip-suggest-embeddings', action='store_true',
            help='Skip candidate embedding maintenance for bulk/initial loads.',
        )
        return parser

    def execute(self, *args, **options):
        nested = _inside_load.get()
        token = _inside_load.set(True)
        try:
            result = super().execute(*args, **options)
        finally:
            _inside_load.reset(token)

        # Athena invokes several mapping loaders. Only its successful outer
        # call may dispatch; otherwise workers can observe a partial load.
        if (not nested and not options.get('dry_run')
                and not options.get('skip_suggest_embeddings')):
            dispatch_suggest_embeddings()
        return result
