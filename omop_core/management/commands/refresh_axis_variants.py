"""Find source codes whose units imply more than one LOINC, and flag them.

The batch form of the refresh the source-code upload runs for the codes it
touches. Candidates are the codes with two or more units in any evidence
source -- imported unit evidence, per-patient source codes, stored
Measurement rows -- plus every code already flagged, so a code that stopped
varying is cleared.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count, F, Func, IntegerField

from omop_core.mapping import axis_variants
from omop_core.models import Measurement, PatientSourceCode, SourceCodeConceptMapping


class _JsonLength(Func):
    function = 'jsonb_array_length'
    output_field = IntegerField()


class Command(BaseCommand):
    help = 'Refresh LOINC axis variants for source codes seen in more than one unit.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Report the codes that would be refreshed; write nothing.')

    def handle(self, *args, **options):
        threshold = axis_variants.FLAG_THRESHOLD
        keys = set(
            SourceCodeConceptMapping.objects
            .annotate(units=_JsonLength(F('source_unit_evidence')))
            .filter(units__gte=threshold)
            .values_list('source_vocabulary_id', 'source_code')
        )
        keys |= set(
            SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0)
            .values_list('source_vocabulary_id', 'source_code')
        )
        keys |= set(
            PatientSourceCode.objects.exclude(source_unit='')
            .values('source_vocabulary_id', 'source_value')
            .annotate(units=Count('source_unit', distinct=True))
            .filter(units__gte=threshold)
            .values_list('source_vocabulary_id', 'source_value')
        )
        # Stored rows carry no vocabulary, so their source values are matched
        # against measurement mappings by code.
        multi_unit_values = set(
            Measurement.objects.exclude(unit_source_value__isnull=True)
            .exclude(unit_source_value='')
            .values('measurement_source_value')
            .annotate(units=Count('unit_source_value', distinct=True))
            .filter(units__gte=threshold)
            .values_list('measurement_source_value', flat=True)
        )
        if multi_unit_values:
            keys |= set(
                SourceCodeConceptMapping.objects
                .filter(omop_table='measurement', source_code__in=multi_unit_values)
                .values_list('source_vocabulary_id', 'source_code')
            )
        self.stdout.write(f'{len(keys)} source codes have more than one unit.')
        if options['dry_run']:
            return
        refreshed = axis_variants.refresh_codes(keys)
        flagged = SourceCodeConceptMapping.objects.filter(axis_variant_count__gt=0).count()
        self.stdout.write(f'Refreshed {refreshed} mappings; {flagged} now have axis variants.')
