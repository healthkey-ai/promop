"""Report approved Measurement mappings whose stored units do not fit LOINC."""
from django.core.management.base import BaseCommand

from omop_core.models import SourceCodeConceptMapping
from omop_core.services.mapping_unit_consistency import unit_consistency_for_mappings


class Command(BaseCommand):
    help = 'Audit approved LOINC Measurement mappings against units already stored for their source codes'

    def handle(self, *args, **options):
        mappings = list(
            SourceCodeConceptMapping.objects
            .filter(
                status='approved', omop_table='measurement',
                target_concept__vocabulary_id='LOINC',
                target_concept__domain_id='Measurement',
            )
            .select_related('target_concept', 'organization')
            .order_by('source_vocabulary_id', 'source_code', 'pk')
        )
        warnings = [
            report for report in unit_consistency_for_mappings(mappings)
            if report['warning']
        ]
        if not warnings:
            self.stdout.write(self.style.SUCCESS(
                'No approved LOINC Measurement mapping conflicts with its stored source units.'
            ))
            return

        for report in warnings:
            expected = ', '.join(report['expected_units']) or '(no example/canonical units)'
            observed = ', '.join(
                f"{row['unit']} ({row['count']})"
                for row in report['observed_units'] if not row['compatible']
            )
            self.stdout.write(
                f"{report['source_vocabulary_id']}:{report['source_code']} -> "
                f"{report['destination_concept_id']} \"{report['destination_concept_name']}\" "
                f"property={report['property'] or '(not quantitative/unknown)'} "
                f"expected={expected}; observed={observed}"
            )
        self.stdout.write(self.style.WARNING(
            f'{len(warnings)} approved mapping(s) have incompatible or unrecognized stored units.'
        ))
        raise SystemExit(1)
