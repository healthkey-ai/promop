"""
Export a patient's OMOP data as a FHIR R4 Bundle JSON file.

Usage:
    # Single patient → file
    python manage.py export_fhir_bundle --person-id 123 --output patient_123.json

    # All patients in an org → file (JSON array of bundles)
    python manage.py export_fhir_bundle --org acme-onc --output acme_patients.json

    # Single patient → stdout (for piping)
    python manage.py export_fhir_bundle --person-id 123
"""

import json
import sys

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import PatientRecord, Person
from omop_core.services.fhir_export import build_fhir_bundle


class Command(BaseCommand):
    help = 'Export OMOP patient data as a FHIR R4 Bundle JSON file'

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            '--person-id',
            type=int,
            help='Export a single patient by person_id',
        )
        group.add_argument(
            '--org',
            type=str,
            help='Export all patients in an organization (by slug)',
        )
        parser.add_argument(
            '-o', '--output',
            type=str,
            default=None,
            help='Output file path (default: stdout)',
        )

    def handle(self, *args, **options):
        person_id = options['person_id']
        org_slug = options['org']
        output_path = options['output']

        if person_id is not None:
            self._export_single(person_id, output_path)
        else:
            self._export_org(org_slug, output_path)

    def _export_single(self, person_id, output_path):
        try:
            person = Person.objects.get(person_id=person_id)
        except Person.DoesNotExist:
            raise CommandError(f'Person with person_id={person_id} does not exist.')

        bundle = build_fhir_bundle(person)
        json_str = json.dumps(bundle, indent=2)

        if output_path:
            with open(output_path, 'w') as f:
                f.write(json_str)
            self.stderr.write(self.style.SUCCESS(
                f'Exported {bundle["total"]} resources for person_id={person_id} → {output_path}'
            ))
        else:
            sys.stdout.write(json_str)
            sys.stdout.write('\n')
            self.stderr.write(self.style.SUCCESS(
                f'Exported {bundle["total"]} resources for person_id={person_id}'
            ))

    def _export_org(self, org_slug, output_path):
        records = (
            PatientRecord.objects
            .filter(organization__slug=org_slug)
            .select_related('person')
        )
        if not records.exists():
            raise CommandError(
                f'No patients found for organization slug="{org_slug}".'
            )

        bundles = []
        for record in records.iterator():
            bundle = build_fhir_bundle(record.person)
            bundles.append(bundle)

        json_str = json.dumps(bundles, indent=2)

        if output_path:
            with open(output_path, 'w') as f:
                f.write(json_str)
            self.stderr.write(self.style.SUCCESS(
                f'Exported {len(bundles)} patients for org="{org_slug}" → {output_path}'
            ))
        else:
            sys.stdout.write(json_str)
            sys.stdout.write('\n')
            self.stderr.write(self.style.SUCCESS(
                f'Exported {len(bundles)} patients for org="{org_slug}"'
            ))
