"""
Django management command to populate PatientRecord from OMOP tables.

Calls omop_core.services.patient_record_service.refresh_patient_record() for each person,
which is the single authoritative derivation path shared with Django signals and the
FHIR upload endpoint.

Usage:
    python manage.py populate_patient_record
    python manage.py populate_patient_record --person-id 4001
    python manage.py populate_patient_record --person-ids 4001,4002
    python manage.py populate_patient_record --org-slugs synthea-bc --limit 10
    python manage.py populate_patient_record --force-update --verbose
"""

from django.core.management.base import BaseCommand
from omop_core.models import PatientRecord, Person
from omop_core.services.patient_record_service import (
    refresh_patient_record,
    _get_demographics,
    _get_treatment_data,
    _get_cll_data,
    _get_lymphoma_data,
    _compute_derived_fields,
    _compute_lymphocyte_doubling_time,
)


class Command(BaseCommand):
    help = 'Populate PatientRecord from OMOP tables for all persons'

    @staticmethod
    def _cohort_persons(person_id=None, person_ids=None, org_slugs=None, limit=None):
        if person_id is not None:
            qs = Person.objects.filter(person_id=person_id).order_by('person_id')
        elif person_ids:
            qs = Person.objects.filter(person_id__in=person_ids).order_by('person_id')
        elif org_slugs:
            scoped_ids = list(
                PatientRecord.objects
                .filter(organization__slug__in=org_slugs)
                .order_by('person_id')
                .values_list('person_id', flat=True)
            )
            qs = Person.objects.filter(person_id__in=scoped_ids).order_by('person_id')
        else:
            qs = Person.objects.all().order_by('person_id')

        return qs[:limit] if limit else qs

    def get_demographics(self, person):
        return _get_demographics(person)

    def get_treatment_data(self, person):
        return _get_treatment_data(person)

    def get_cll_data(self, person):
        return _get_cll_data(person)

    def get_lymphoma_data(self, person):
        return _get_lymphoma_data(person)

    def _compute_derived_fields(self, patient_info):
        return _compute_derived_fields(patient_info)

    @staticmethod
    def _compute_lymphocyte_doubling_time(alc_points):
        return _compute_lymphocyte_doubling_time(alc_points)

    def add_arguments(self, parser):
        parser.add_argument(
            '--person-id',
            type=int,
            help='Process specific person ID only',
        )
        parser.add_argument(
            '--person-ids',
            default='',
            help='Comma-separated person IDs to process',
        )
        parser.add_argument(
            '--org-slugs',
            default='',
            help='Comma-separated organization slugs to scope the refresh to existing PatientRecords',
        )
        parser.add_argument(
            '--limit',
            type=int,
            default=None,
            help='Maximum number of people to process after scoping',
        )
        parser.add_argument(
            '--force-update',
            action='store_true',
            help='Force update (always refreshes, ignored — refresh_patient_record always upserts)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed processing information',
        )

    def handle(self, *args, **options):
        person_id = options.get('person_id')
        person_ids_arg = options.get('person_ids', '')
        org_slugs_arg = options.get('org_slugs', '')
        limit = options.get('limit')
        verbose = options.get('verbose')

        person_ids = [int(value) for value in person_ids_arg.split(',') if value.strip()]
        org_slugs = [value.strip() for value in org_slugs_arg.split(',') if value.strip()]

        if person_id is not None and person_ids:
            self.stdout.write(self.style.ERROR('Pass either --person-id or --person-ids, not both'))
            return

        persons = self._cohort_persons(
            person_id=person_id,
            person_ids=person_ids,
            org_slugs=org_slugs,
            limit=limit,
        )
        if not persons.exists():
            if person_id is not None:
                self.stdout.write(self.style.ERROR(f'Person with ID {person_id} not found'))
            else:
                self.stdout.write(self.style.ERROR('No matching persons found for the requested cohort'))
            return

        total = persons.count()
        self.stdout.write(f'Processing {total} person(s)…')

        created = updated = errors = 0

        for person in persons:
            try:
                existed = hasattr(person, 'patient_record')
                refresh_patient_record(person)
                if existed:
                    updated += 1
                else:
                    created += 1
                if verbose:
                    self.stdout.write(f'  Person {person.person_id}: {"updated" if existed else "created"}')
            except Exception as exc:
                errors += 1
                self.stdout.write(self.style.ERROR(f'  Person {person.person_id}: {exc}'))

        self.stdout.write(self.style.SUCCESS(
            f'Done — created: {created}, updated: {updated}, errors: {errors}'
        ))
