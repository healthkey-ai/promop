"""
Django management command to populate PatientInfo from OMOP tables.

Calls omop_core.services.patient_info_service.refresh_patient_info() for each person,
which is the single authoritative derivation path shared with Django signals and the
FHIR upload endpoint.

Usage:
    python manage.py populate_patient_info
    python manage.py populate_patient_info --person-id 4001
    python manage.py populate_patient_info --force-update --verbose
"""

from django.core.management.base import BaseCommand
from omop_core.models import Person
from omop_core.services.patient_info_service import (
    refresh_patient_info,
    _get_demographics,
    _get_treatment_data,
    _get_cll_data,
    _get_lymphoma_data,
    _compute_derived_fields,
    _compute_lymphocyte_doubling_time,
)


class Command(BaseCommand):
    help = 'Populate PatientInfo from OMOP tables for all persons'

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
            '--force-update',
            action='store_true',
            help='Force update (always refreshes, ignored — refresh_patient_info always upserts)',
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='Show detailed processing information',
        )

    def handle(self, *args, **options):
        person_id = options.get('person_id')
        verbose = options.get('verbose')

        if person_id:
            persons = Person.objects.filter(person_id=person_id)
            if not persons.exists():
                self.stdout.write(self.style.ERROR(f'Person with ID {person_id} not found'))
                return
        else:
            persons = Person.objects.all()

        total = persons.count()
        self.stdout.write(f'Processing {total} person(s)…')

        created = updated = errors = 0

        for person in persons:
            try:
                existed = hasattr(person, 'patientinfo')
                refresh_patient_info(person)
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
