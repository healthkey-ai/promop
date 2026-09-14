"""Read-only audit of the complete effective genomic component inventory.

Missing vocabulary, recipes or resolutions are incomplete audits (exit 1),
not certification. Repair approved recipes through field-mapping curation,
then rerun this command. No patient facts or curator decisions are changed.
"""
from django.core.management.base import BaseCommand, CommandError
from rest_framework.exceptions import ValidationError

from omop_core.models import Concept, FieldConceptMapping
from omop_core.services.genomics import _event_concept, approved_mapping, mapped_concept
from omop_core.services.genomics_catalog import patient_fields
from omop_core.services.genomics_components import components
from omop_core.services.genomics_vocabulary import resolve_loinc


class Command(BaseCommand):
    help = 'Audit every genomic component recipe against installed vocabulary; incomplete audits fail.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--include-writer-prerequisites', action='store_true',
            help='Also check all priority parent recipes and the unmapped, actor type and CDM event concepts.',
        )

    def audit_writer_prerequisites(self):
        incomplete = parents = local = 0
        for field in patient_fields():
            try:
                mapping = approved_mapping(field)
            except ValidationError:
                incomplete += 1
                self.stdout.write(f'{field}: missing or incomplete approved parent recipe')
                continue
            if mapping.omop_table != 'measurement':
                incomplete += 1
                self.stdout.write(f'{field}: parent recipe must use measurement for event linking')
                continue
            concept_id, _ = mapped_concept(mapping)
            parents += 1
            local += concept_id == 0
        # These are the interactive writer's actor defaults, independent of
        # recipe type settings. Service callers may supply additional type IDs.
        for concept_id, role in ((0, 'unmapped'), (32817, 'clinical/service actor'),
                                 (32865, 'patient/representative actor')):
            if not Concept.objects.filter(pk=concept_id, invalid_reason__isnull=True).exists():
                incomplete += 1
                self.stdout.write(f'{role}: required active concept {concept_id} unavailable')
        try:
            event_id = _event_concept()
        except ValidationError:
            incomplete += 1
            self.stdout.write('CDM event: active measurement.measurement_id identity unavailable')
        else:
            self.stdout.write(f'CDM event: measurement.measurement_id resolves to {event_id}')
        self.stdout.write(
            f'{parents} priority parent recipes usable, {local} use concept 0 with source identity; '
            f'{incomplete} writer prerequisites incomplete.')
        return incomplete

    def handle(self, *args, **options):
        writer_incomplete = (self.audit_writer_prerequisites()
                             if options['include_writer_prerequisites'] else 0)
        mappings = {m.field_name: m for m in FieldConceptMapping.objects.filter(
            field_name__startswith='genetic_mutations.',
        ).select_related('concept')}
        incomplete = mismatches = resolved = local = 0
        for attribute in components():
            field = 'genetic_mutations.' + attribute['key']
            mapping = mappings.get(field)
            portable = attribute.get('concept_code') or (
                attribute['code'] if not attribute['code'].startswith('genomics:') else '')
            if mapping is None or mapping.status != 'approved':
                incomplete += 1
                self.stdout.write(f'{field}: missing approved recipe')
                continue
            if (mapping.omop_table not in ('measurement', 'observation')
                    or not mapping.source_value or len(mapping.source_value) > 50
                    or mapping.value_kind != attribute['value_kind']):
                incomplete += 1
                self.stdout.write(f'{field}: incomplete or incompatible storage recipe')
                continue
            if mapping.vocabulary_id != 'LOINC' or not mapping.concept_code:
                if portable:
                    incomplete += 1
                    self.stdout.write(f'{field}: portable LOINC {portable} recipe requires curation')
                else:
                    local += 1
                continue
            result = resolve_loinc(mapping.concept_code)
            if result.problem:
                incomplete += 1
                self.stdout.write(f'{field}: LOINC {mapping.concept_code}: {result.problem}')
                continue
            resolved += 1
            target = result.standard
            expected = target.domain_id.lower()
            self.stdout.write(
                f'{field}: source {result.source.pk} ({result.source.domain_id}) -> '
                f'standard {target.pk} ({target.domain_id}); recipe {mapping.omop_table}')
            if (mapping.omop_table != expected
                    or mapping.concept_id not in (None, 0, target.pk)):
                mismatches += 1
                self.stdout.write(
                    f'  REVIEW REPAIR: omop_table={expected}, concept={target.pk}; '
                    f'preserve source_value={mapping.source_value!r} and other curator settings')
        self.stdout.write(f'{resolved} resolved, {local} intentionally local, '
                          f'{incomplete} incomplete, {mismatches} mismatches.')
        if incomplete or mismatches or writer_incomplete:
            raise CommandError('Genomics domain audit not verified. Review field-mapping recipes and required vocabulary, then rerun.')
        self.stdout.write(self.style.SUCCESS('All component domain assignments verified against installed vocabulary.'))
        if options['include_writer_prerequisites']:
            self.stdout.write(self.style.SUCCESS('Writer parent mappings and required concepts verified.'))
