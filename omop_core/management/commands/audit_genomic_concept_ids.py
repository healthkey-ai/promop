"""Read-only audit of the wrong genomic origin/interpretation concept ids.

The old writer/derivation maps stored SNOMED codes in OMOP concept-id columns:
`30166007` is "Struck by", `10828004` is "Positive", `42425007` is "Equivocal"
and `255395001` is "Origins". Facts written through that path carry the wrong
`qualifier_concept_id` / `value_as_concept_id` (cancerbot #5671).

This command reports them; it never writes. Repair is a separate, explicitly
authorized operation.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count, Q

from omop_core.models import Concept, Measurement
from omop_core.services.patient_record_service import _GENETIC_MUTATION_LOINCS


WRONG_ORIGIN_IDS = (255395001, 255461003)
WRONG_INTERPRETATION_IDS = (30166007, 10828004, 42425007)
_GENOMIC_CODES = tuple(_GENETIC_MUTATION_LOINCS) + ('36908-2', '81252-9')


def _wrong_rows():
    genomic = (
        Q(measurement_source_value__startswith='genomics:')
        | Q(measurement_source_value='cb-profile:genetic-mutation')
        # Source-only facts can carry the gene LOINC in measurement_source_value
        # with measurement_concept_id=0; the derivation discovers them through
        # this value, so the audit must too.
        | Q(measurement_source_value__in=_GENOMIC_CODES)
        | Q(measurement_concept__concept_code__in=_GENOMIC_CODES)
    )
    return Measurement.objects.filter(
        genomic
        & (
            Q(qualifier_concept_id__in=WRONG_ORIGIN_IDS)
            | Q(value_as_concept_id__in=WRONG_INTERPRETATION_IDS)
        )
    )


class Command(BaseCommand):
    help = 'Report genomic Measurements that carry the wrong origin/interpretation concept ids.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=20, help='Sample rows to print.')

    def handle(self, *args, **options):
        rows = (
            _wrong_rows()
            .values('qualifier_concept_id', 'value_as_concept_id')
            .annotate(n=Count('measurement_id'))
            .order_by('-n')
        )
        names = {
            concept.concept_id: concept.concept_name
            for concept in Concept.objects.filter(
                concept_id__in=WRONG_ORIGIN_IDS + WRONG_INTERPRETATION_IDS,
            )
        }
        total = 0
        self.stdout.write('wrong genomic concept ids (read-only audit)')
        for row in rows:
            total += row['n']
            origin = row['qualifier_concept_id']
            interpretation = row['value_as_concept_id']
            self.stdout.write(
                f"  origin={origin} ({names.get(origin, 'unknown')}) "
                f"interpretation={interpretation} ({names.get(interpretation, 'unknown')}) "
                f"measurements={row['n']}"
            )
        if total == 0:
            self.stdout.write('  none')
            return

        self.stdout.write(f'total measurements: {total}')
        sample = list(
            _wrong_rows().values_list('measurement_id', flat=True)[:options['limit']]
        )
        self.stdout.write(f'sample measurement ids: {sample}')
        self.stdout.write('No rows were changed. Repair requires separate authorization.')
