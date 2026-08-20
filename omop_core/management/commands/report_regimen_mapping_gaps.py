"""Report regimen/drug mapping gaps recorded at ingest time (issue #236).

Every unmatched regimen/drug name seen by an ingest path is quarantined under
an HK-* vocabulary and tracked in the regimen_mapping_gap table.  This command
is the curation queue: names listed here need either an upstream vocabulary
addition (future HemOnc release) or a HealthKey-authored concept.

Usage:
    python manage.py report_regimen_mapping_gaps
    python manage.py report_regimen_mapping_gaps --status unmatched
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from omop_core.models import RegimenMappingGap


class Command(BaseCommand):
    help = "Report regimen/drug mapping gaps (unmatched names quarantined under HK-* vocabularies)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--status',
            choices=[c for c, _ in RegimenMappingGap.STATUS_CHOICES],
            default=None,
            help='Only show gaps with this status.',
        )

    def handle(self, *args, **options):
        status_filter = options['status']

        counts = dict(
            RegimenMappingGap.objects.values_list('status')
            .annotate(n=Count('id'))
            .values_list('status', 'n')
        )
        self.stdout.write('Counts by status:')
        for code, _label in RegimenMappingGap.STATUS_CHOICES:
            self.stdout.write(f'  {code:<10} {counts.get(code, 0)}')

        qs = RegimenMappingGap.objects.order_by('-occurrence_count', 'normalized_name')
        if status_filter:
            qs = qs.filter(status=status_filter)

        rows = list(qs.values(
            'status', 'source_system', 'source_value',
            'quarantine_concept_id', 'matched_concept_id',
            'occurrence_count', 'last_seen',
        ))
        if not rows:
            self.stdout.write('\nNo mapping gaps recorded.')
            return

        self.stdout.write(
            f'\n{"status":<10} {"source_system":<14} {"occurrences":>11} '
            f'{"quarantine_id":>13} {"matched_id":>10}  source_value'
        )
        self.stdout.write('-' * 100)
        for r in rows:
            self.stdout.write(
                f'{r["status"]:<10} {r["source_system"]:<14} '
                f'{r["occurrence_count"]:>11} '
                f'{r["quarantine_concept_id"] or "-":>13} '
                f'{r["matched_concept_id"] or "-":>10}  '
                f'{r["source_value"]}'
            )
