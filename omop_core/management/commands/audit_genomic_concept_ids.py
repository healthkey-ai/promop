"""Read-only audit of genomic concept ids.

The old writer/derivation maps stored SNOMED codes in OMOP concept-id columns:
`30166007` is "Struck by", `10828004` is "Positive", `42425007` is "Equivocal"
and `255395001` is "Origins". Facts written through that path carry the wrong
`qualifier_concept_id` / `value_as_concept_id` (cancerbot #5671).

The second section reports gene identity (#5673): catalog genes whose reviewed
OMOP Genomic concept is absent or unusable, and variant parents still carrying
the pre-migration generic/unmapped concept.

This command reports; it never writes. Repair is a separate, explicitly
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


def _usable_gene_concept(concept):
    """The same gate the writer applies to a mapped genomics parent."""
    return bool(concept is not None
                and concept.vocabulary_id == 'OMOP Genomic'
                and concept.domain_id.lower() == 'measurement'
                and concept.standard_concept == 'S'
                and concept.invalid_reason is None)


def _gene_identity():
    """(gene count, catalog gaps, variant parents off their gene concept)."""
    from omop_core.services.genomics_catalog import marker_sources, markers
    genes = [m for m in markers() if m.get('kind') == 'gene']
    loaded = {
        concept.concept_id: concept for concept in Concept.objects.filter(
            concept_id__in=[m.get('omop_concept_id') for m in genes if m.get('omop_concept_id')],
        )
    }
    gaps = [
        (m['field_name'], m.get('omop_concept_id'))
        for m in genes if not _usable_gene_concept(loaded.get(m.get('omop_concept_id')))
    ]
    expected = {
        source: m['omop_concept_id'] for source, m in marker_sources().items()
        if m.get('kind') == 'gene' and m.get('omop_concept_id')
    }
    rows = (
        Measurement.objects
        .filter(measurement_source_value__in=list(expected), is_erroneous=False)
        .values('measurement_source_value', 'measurement_concept_id')
        .annotate(n=Count('measurement_id'))
        .order_by('-n')
    )
    stale = [
        (row['measurement_source_value'], row['measurement_concept_id'], row['n'])
        for row in rows
        if expected.get(row['measurement_source_value']) != row['measurement_concept_id']
    ]
    return len(genes), gaps, stale, expected


class Command(BaseCommand):
    help = 'Report genomic Measurements that carry wrong or missing concept ids.'

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
        else:
            self.stdout.write(f'total measurements: {total}')
            sample = list(
                _wrong_rows().values_list('measurement_id', flat=True)[:options['limit']]
            )
            self.stdout.write(f'sample measurement ids: {sample}')

        gene_count, gaps, stale, expected = _gene_identity()
        self.stdout.write('')
        self.stdout.write('gene identity (read-only audit)')
        if not gaps:
            self.stdout.write(
                f'  catalog genes: {gene_count}; every gene has a loaded standard OMOP Genomic concept'
            )
        else:
            self.stdout.write(f'  catalog genes: {gene_count}; missing/unusable concepts: {len(gaps)}')
            for field_name, concept_id in gaps[:options['limit']]:
                self.stdout.write(f'  {field_name}: concept_id={concept_id}')
        if not stale:
            self.stdout.write('  variant parents: all carry their gene concept')
        else:
            self.stdout.write(f'  variant parents off their gene concept: {len(stale)} groups')
            for source, concept_id, n in stale[:options['limit']]:
                self.stdout.write(
                    f'  {source}: measurement_concept_id={concept_id} '
                    f'expected={expected.get(source)} measurements={n}'
                )
        self.stdout.write('')
        self.stdout.write('No rows were changed. Repair requires separate authorization.')
