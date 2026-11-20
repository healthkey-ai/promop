"""Move stored clinical rows still queued on approved mappings (#1952).

A mapping save queues its re-point in ``deferred_repoint_concept_ids`` and a job
moves the rows. A job that failed past its retries leaves them queued; this runs
them. So does a patient-page approval, which deliberately leaves other patients'
rows for Generate OMOP, so ``--all`` moves those for every patient too.
"""
from django.core.management.base import BaseCommand, CommandError

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.repoint_jobs import run_repoint


class Command(BaseCommand):
    help = 'Move clinical rows still queued on approved code mappings'

    def add_arguments(self, parser):
        parser.add_argument('mapping_ids', nargs='*', type=int, help='Mappings to run.')
        parser.add_argument('--all', action='store_true',
                            help='Every approved mapping with queued rows, patient-page approvals included.')
        parser.add_argument('--dry-run', action='store_true', help='List what would run.')

    def handle(self, *args, mapping_ids, all, dry_run, **options):
        if not mapping_ids and not all:
            raise CommandError('Name mapping ids, or pass --all.')
        queued = SourceCodeConceptMapping.objects.filter(status='approved').exclude(
            deferred_repoint_concept_ids=[]).order_by('pk')
        if mapping_ids:
            queued = queued.filter(pk__in=mapping_ids)
        for mapping in queued:
            line = f'{mapping.pk} {mapping.source_vocabulary_id}:{mapping.source_code} ' \
                   f'from {mapping.deferred_repoint_concept_ids} to {mapping.target_concept_id}'
            if dry_run:
                self.stdout.write(line)
                continue
            totals = run_repoint(mapping.pk)
            self.stdout.write(f'{line}: {totals}')
