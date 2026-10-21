"""Find source codes whose units imply more than one LOINC, and flag them.

The batch form of the refresh the source-code upload runs for the codes it
touches; migration 0300 ran it once on every deployment. Candidates are
:func:`axis_variants.multi_unit_keys`.
"""
from django.core.management.base import BaseCommand

from omop_core.mapping import axis_variants
from omop_core.models import SourceCodeConceptMapping


class Command(BaseCommand):
    help = 'Refresh LOINC axis variants for source codes seen in more than one unit.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the codes that would be refreshed; write nothing.')

    def handle(self, *args, **options):
        keys = axis_variants.multi_unit_keys()
        self.stdout.write(f'{len(keys)} source codes have more than one unit.')
        if options['dry_run']:
            return
        refreshed = axis_variants.refresh_all(keys)
        flagged = SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0).count()
        self.stdout.write(f'Refreshed {refreshed} mappings; {flagged} now have axis variants.')
