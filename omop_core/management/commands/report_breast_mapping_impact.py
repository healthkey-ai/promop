"""Read-only inventory for the confirmed breast cancer wrong-code paths (#1227)."""

import json

from django.core.management.base import BaseCommand
from django.db.models import Q

from omop_core.models import (
    FieldConceptMapping, Measurement, Observation, PatientRecord, SourceCodeConceptMapping,
)
from omop_core.services.breast_mapping_safety import (
    WRONG_FIELD_LOINCS, wrong_breast_field_mappings,
)


class Command(BaseCommand):
    help = 'Report potential breast mapping impact as aggregate JSON; never changes data.'

    def handle(self, *args, **options):
        report = {
            'issue': 1227,
            'mode': 'read-only',
            'interpretation': (
                'Potential exposure, not confirmed corruption. Genuine HER2, ER and '
                'perineural results also match. Review original source evidence before '
                'reconciliation; do not relabel these facts automatically.'
            ),
            'fields': {},
        }
        for field, codes in WRONG_FIELD_LOINCS.items():
            measurement_rows = Measurement.objects.filter(
                Q(measurement_concept__vocabulary_id='LOINC',
                  measurement_concept__concept_code__in=codes)
                | Q(measurement_source_value__in=codes),
                is_erroneous=False,
            )
            observation_rows = Observation.objects.filter(
                Q(observation_concept__vocabulary_id='LOINC',
                  observation_concept__concept_code__in=codes)
                | Q(observation_source_value__in=codes),
                is_erroneous=False,
            )
            exposed_records = PatientRecord.objects.filter(
                Q(person_id__in=measurement_rows.values('person_id'))
                | Q(person_id__in=observation_rows.values('person_id')),
            )
            report['fields'][field] = {
                'suspect_loincs': list(codes),
                'measurement_rows': measurement_rows.count(),
                'observation_rows': observation_rows.count(),
                'persons_with_measurements': measurement_rows.values('person_id').distinct().count(),
                'exposed_patient_records': exposed_records.count(),
                'exposed_records_with_stored_value': exposed_records.exclude(
                    **{f'{field}__isnull': True},
                ).count(),
                'exposed_records_with_pending_edit': exposed_records.filter(
                    user_edited_fields__contains=[field],
                ).count(),
            }
        report['invalid_field_recipes'] = list(
            FieldConceptMapping.objects.filter(wrong_breast_field_mappings())
            .order_by('field_name').values('field_name', 'status', 'concept_id', 'concept_code', 'source_value')
        )
        # Uncoded HK-Labs aliases were seeded against ER in migration 0201.
        # Count for review, including target metadata rather than assuming IDs.
        report['suspect_ki67_source_mappings'] = SourceCodeConceptMapping.objects.filter(
            target_concept__vocabulary_id='LOINC',
            target_concept__concept_code__in=['85319-2', '85337-4'],
        ).filter(
            Q(source_code__iregex=r'ki[- ]?67')
            | Q(source_code_description__iregex=r'ki[- ]?67'),
        ).count()
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
