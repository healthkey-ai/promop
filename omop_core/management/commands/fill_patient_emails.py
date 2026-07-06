"""
Management command to fill PatientRecord.email from patient name.

Generates a placeholder email address for patients who have no email recorded.
The default pattern is: <first_initial><last_name>@<domain>

Usage:
    python manage.py fill_patient_emails --domain example.com
    python manage.py fill_patient_emails --domain example.com --overwrite
    python manage.py fill_patient_emails --domain example.com --dry-run
"""

from django.core.management.base import BaseCommand
from omop_core.models import PatientRecord


class Command(BaseCommand):
    help = "Fill PatientRecord.email with a generated placeholder address derived from the patient name"

    def add_arguments(self, parser):
        parser.add_argument(
            '--domain',
            default='example.com',
            help='Email domain to use for generated addresses (default: example.com)',
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing non-blank email values',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print what would be updated without making changes',
        )

    def handle(self, *args, **options):
        overwrite = options['overwrite']
        dry_run = options['dry_run']
        domain = options['domain']

        from django.db.models import Q
        if overwrite:
            qs = PatientRecord.objects.select_related('person').all()
        else:
            qs = PatientRecord.objects.select_related('person').filter(
                Q(email__isnull=True) | Q(email='')
            )

        updated = 0
        skipped = 0

        for patient in qs:
            person = patient.person
            first = (person.given_name or '').strip()
            last = (person.family_name or '').strip()

            if not first and not last:
                self.stdout.write(
                    self.style.WARNING(
                        f"  Person {person.person_id}: no name — skipping"
                    )
                )
                skipped += 1
                continue

            first_initial = first[0].lower() if first else ''
            last_slug = last.lower().replace(' ', '') if last else f'person{person.person_id}'
            email = f"{first_initial}{last_slug}@{domain}"

            if dry_run:
                self.stdout.write(f"  [dry-run] Person {person.person_id}: {first} {last} → {email}")
            else:
                patient.email = email
                patient.save(update_fields=['email'])
                self.stdout.write(f"  Person {person.person_id}: {first} {last} → {email}")

            updated += 1

        action = "Would update" if dry_run else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{action} {updated} patient(s). Skipped {skipped} (no name)."
            )
        )
