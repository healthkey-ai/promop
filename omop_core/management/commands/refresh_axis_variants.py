"""Find source codes whose units imply more than one LOINC, and flag them.

The batch form of the refresh the source-code upload runs for the codes it
touches. Candidates are :func:`axis_variants.multi_unit_keys`.

Inline by default. ``--queue`` records a durable request for the worker
instead, which is how a deployment runs it (#2021): on a large instance the
walk takes longer than a release step may hold.
"""
from django.core.management.base import BaseCommand

from omop_core.mapping import axis_variants
from omop_core.models import SourceCodeConceptMapping


class Command(BaseCommand):
    help = 'Refresh LOINC axis variants for source codes seen in more than one unit.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the codes that would be refreshed; write nothing.')
        parser.add_argument('--queue', action='store_true',
                            help='Record a request for the Celery worker instead of refreshing here.')

    def handle(self, *args, **options):
        if options['queue']:
            from omop_core.services.axis_variant_jobs import request_refresh

            request, created = request_refresh(reason='manage.py refresh_axis_variants --queue')
            if created:
                self.stdout.write(self.style.SUCCESS(
                    f'Queued axis variant refresh {request.pk}; the worker performs it.'))
            else:
                self.stdout.write(
                    f'Axis variant refresh {request.pk} is already {request.outcome}; not queued twice.')
            return
        keys = axis_variants.multi_unit_keys()
        self.stdout.write(f'{len(keys)} source codes have more than one unit.')
        if options['dry_run']:
            return
        refreshed = axis_variants.refresh_all(keys)
        flagged = SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0).count()
        self.stdout.write(f'Refreshed {refreshed} mappings; {flagged} now have axis variants.')
