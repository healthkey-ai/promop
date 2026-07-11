"""
Delete an organization and every patient attached to it.

This command removes:
  - the Organization row
  - each PatientRecord row for that organization
  - each linked Person row
  - each linked Identity / PatientUser pair
  - OMOP and patient-scoped rows that are not removed automatically by Person.delete()

Usage:
    python manage.py delete_org_patients --org <slug-or-id> --confirm
    python manage.py delete_org_patients --org <slug-or-id> --dry-run
"""

from django.core.management.base import BaseCommand, CommandError

from omop_core.models import Organization, PatientRecord
from omop_core.services.organization_cleanup import delete_organization_with_patient_cascade
from patient_portal.models import PatientUser


class Command(BaseCommand):
    help = "Delete an organization, its patients, and all associated patient data"

    def add_arguments(self, parser):
        parser.add_argument(
            '--org',
            required=True,
            help='Organization slug or integer primary key',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without changing the database',
        )
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Required for destructive execution',
        )

    def handle(self, *args, **options):
        org = self._resolve_org(options['org'])
        dry_run = options['dry_run']
        confirm = options['confirm']

        patient_records = list(
            PatientRecord.objects.filter(organization=org).select_related('person')
        )
        person_ids = [record.person_id for record in patient_records]
        patient_users = {
            patient_user.person_id: patient_user
            for patient_user in PatientUser.objects.filter(person_id__in=person_ids).select_related('identity')
        }

        self.stdout.write(f"Organization: {org.name} (slug={org.slug}, id={org.pk})")
        self.stdout.write(f"PatientRecords: {len(patient_records)}")
        self.stdout.write(f"Linked identities: {len(patient_users)}")

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run only; nothing was deleted."))
            return

        if not confirm:
            raise CommandError("This command is destructive. Re-run with --confirm to delete data.")

        delete_organization_with_patient_cascade(org)

        self.stdout.write(self.style.SUCCESS(
            f"Deleted organization {org.slug!r} and {len(patient_records)} patient record(s)."
        ))

    def _resolve_org(self, org_key):
        try:
            if org_key.isdigit():
                return Organization.objects.get(pk=int(org_key))
            return Organization.objects.get(slug=org_key)
        except Organization.DoesNotExist as exc:
            raise CommandError(f"Organization not found: {org_key!r}") from exc
