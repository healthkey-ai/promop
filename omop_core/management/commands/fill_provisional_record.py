"""Give one person a full provisional view (#2696): propose, materialise, rederive.

For the patient's unresolved and inventory codes: run Suggest (Jev by default)
on the proposed queue rows that have no destination, make the patient's rows
provisional at proposals at or above ``PROVISIONAL_CONFIDENCE_FLOOR``, and
rebuild the patient's record so the provisional tables are filled. See
``omop_core/services/fill_provisional.py``.

Resumable: a code this model version already attempted is skipped on a rerun
(``--retry-declined`` redoes them). Writes to ``DATABASE_URL``: name the
database explicitly. Refuses, writing nothing, when ``PROVISIONAL_DATA_ENABLED``
is off. The ranker's key comes from the environment (``JEV_API_KEY`` or
``ANTHROPIC_API_KEY``).
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from omop_core.mapping.code_resolution import CLINICAL_TABLES
from omop_core.mapping.suggestions import ALL_STRATEGIES, RANKING_MODELS
from omop_core.services import fill_provisional


class Command(BaseCommand):
    help = "Propose mappings for one patient's codes, make their rows provisional, rederive their record"

    def add_arguments(self, parser):
        parser.add_argument('person_id', type=int)
        parser.add_argument('--ranker', choices=sorted(RANKING_MODELS), default='jev',
                            help='The ranking model Suggest uses (default jev).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Propose for at most this many queue rows in this run.')
        parser.add_argument('--tables', nargs='+', choices=sorted(CLINICAL_TABLES), default=None,
                            help='Only the codes held under these clinical tables.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Count what would be proposed; write nothing and call no model.')
        parser.add_argument('--skip-propose', action='store_true',
                            help='Skip Suggest: only make rows provisional and rederive.')
        parser.add_argument('--replace', action='store_true',
                            help='Also redo destinations a previous Suggest run set (never an importer\'s '
                                 'or a curator\'s).')
        parser.add_argument('--retry-declined', action='store_true',
                            help='Also redo codes this model version already attempted.')
        parser.add_argument('--strategies', nargs='+', choices=ALL_STRATEGIES, default=None,
                            help='Retrieval strategies (default: umls lexical vectors).')
        parser.add_argument('--batch-size', type=int, default=fill_provisional.DEFAULT_BATCH,
                            help='Queue rows per Suggest call; its writes land when it ends.')
        parser.add_argument('--show-values', action='store_true',
                            help='Print the clinical values of provisional fields (off by default: '
                                 'job logs should carry no patient values).')

    def handle(self, *args, person_id, ranker, limit, tables, dry_run, skip_propose, replace,
               retry_declined, strategies, batch_size, show_values, **options):
        def log(message):
            self.stdout.write(message)
            self.stdout.flush()
            sys.stdout.flush()

        try:
            summary = fill_provisional.run(
                person_id, ranker=ranker, limit=limit, tables=tables, dry_run=dry_run,
                skip_propose=skip_propose, replace=replace, retry=retry_declined,
                strategies=strategies, batch_size=batch_size, show_values=show_values, log=log)
        except fill_provisional.FillError as exc:
            raise CommandError(str(exc)) from exc
        log(self.style.SUCCESS(f'Done: {summary}'))
