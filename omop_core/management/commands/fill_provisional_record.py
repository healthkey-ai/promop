"""Give one person a full provisional view (#2696): propose, materialise, rederive.

For the patient's codes with unresolved rows (``--include-inventory`` adds the
inventory-only ones): run Suggest (Jev by default) on the proposed queue rows that
have no destination; then resolve every one of the patient's rows against all the
mappings (approved makes rows real, a proposal at or above
``PROVISIONAL_CONFIDENCE_FLOOR`` makes them provisional); then rebuild the
patient's record, ``patient_record`` from the approved rows and the provisional
pair from the proposed ones. See ``omop_core/services/fill_provisional.py``.

Resumable: a code the ranker answered (a destination, or a decline) is skipped
on a rerun (``--retry-declined`` redoes declines); a code it could not rank is
left untouched and retried. ``--tables`` and ``--limit`` narrow only the propose
step: the resolve and the rederive cover every table. Writes to ``DATABASE_URL``: name the database explicitly. Refuses, writing
nothing, when ``PROVISIONAL_DATA_ENABLED`` is off. The ranker's key comes from
the environment (``JEV_API_KEY`` or ``ANTHROPIC_API_KEY``).
"""
import sys

from django.core.management.base import BaseCommand, CommandError

from omop_core.mapping.code_resolution import CLINICAL_TABLES
from omop_core.mapping.suggestions import ALL_STRATEGIES, RANKING_MODELS
from omop_core.services import fill_provisional


class Command(BaseCommand):
    help = ("Propose mappings for one patient's unresolved codes, resolve their rows against every "
            "mapping, rederive their record")

    def add_arguments(self, parser):
        parser.add_argument('person_id', type=int)
        parser.add_argument('--ranker', choices=sorted(RANKING_MODELS), default='jev',
                            help='The ranking model Suggest uses (default jev).')
        parser.add_argument('--limit', type=int, default=None,
                            help='Propose for at most this many queue rows in this run (the propose step only).')
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument('--unresolved-only', action='store_true',
                           help='Propose only for codes that have unresolved clinical rows (the default). '
                                'Inventory-only codes are slow (about four seconds each) and only matter for '
                                'creating rows from the inventory.')
        scope.add_argument('--include-inventory', action='store_true',
                           help='Also propose for inventory-only codes, those the patient has no clinical row '
                                'for yet (on HealthTree one patient: 533 codes with unresolved rows, 2,546 '
                                'inventory-only).')
        parser.add_argument('--tables', nargs='+', choices=sorted(CLINICAL_TABLES), default=None,
                            help='Only propose for codes held under these clinical tables (the propose step only: '
                                 'every row is resolved and the record rederived for all tables).')
        parser.add_argument('--dry-run', action='store_true',
                            help='Count what would be proposed; write nothing and call no model.')
        parser.add_argument('--skip-propose', action='store_true',
                            help='Skip Suggest: only resolve the rows against the mappings that exist, and rederive.')
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
               retry_declined, strategies, batch_size, show_values, include_inventory, **options):
        def log(message):
            self.stdout.write(message)
            self.stdout.flush()
            sys.stdout.flush()

        try:
            summary = fill_provisional.run(
                person_id, ranker=ranker, limit=limit, tables=tables, dry_run=dry_run,
                skip_propose=skip_propose, replace=replace, retry=retry_declined,
                strategies=strategies, batch_size=batch_size, show_values=show_values,
                include_inventory=include_inventory, log=log)
        except fill_provisional.FillError as exc:
            raise CommandError(str(exc)) from exc
        log(self.style.SUCCESS(f'Done: {summary}'))
