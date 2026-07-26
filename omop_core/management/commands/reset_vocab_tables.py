"""DANGER: TRUNCATE every vocabulary corpus table (explicit escape hatch).

Normal Athena loads are atomic stage → validate → publish upserts
(``load_athena_vocabularies``); a full wipe is almost never what you want —
it cascades through every table with a FK to concept, including cdm_source,
observation_period, and the clinical event tables.  This command exists for
disaster recovery only (e.g. a corrupted corpus that validation cannot
reject).  Requires --confirm.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from omop_core.management.commands.load_athena_vocabularies import (
    seed_concept_zero, sync_cdm_source_metadata,
)

CORPUS_TABLES_SQL = (
    'concept_ancestor, concept_relationship, concept_synonym, drug_strength, '
    'source_to_concept_map, concept, concept_class, domain, relationship, vocabulary'
)


class Command(BaseCommand):
    help = 'TRUNCATE all vocabulary corpus tables (destructive; requires --confirm)'

    def add_arguments(self, parser):
        parser.add_argument('--confirm', action='store_true',
                            help='Required — without it the command refuses to run')

    def handle(self, *args, **options):
        if not options['confirm']:
            raise CommandError(
                'reset_vocab_tables truncates every vocabulary corpus table '
                'CASCADE (wiping clinical tables that FK to concept). '
                'Re-run with --confirm to proceed.'
            )
        self.stdout.write('Truncating vocabulary corpus tables (CASCADE)...')
        with connection.cursor() as cur:
            cur.execute(f'TRUNCATE {CORPUS_TABLES_SQL} CASCADE')
        self.stdout.write('  Done. Re-seeding concept 0 and cdm_source...')
        seed_concept_zero(self.stdout.write)
        sync_cdm_source_metadata(self.stdout.write)
        self.stdout.write(
            '  NOTE: CASCADE also cleared every table with a FK to concept — '
            'including cdm_source, observation_period, and clinical event '
            'tables. Re-run populate_observation_period to re-derive '
            'observation periods, then load_athena_vocabularies to repopulate '
            'the corpus.'
        )
