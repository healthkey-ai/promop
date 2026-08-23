"""Re-derive PatientRecord therapy concept IDs from OMOP treatment facts.

This command deliberately does *not* reverse-map display strings stored on
PatientRecord.  Those strings are a derived read model and do not carry the
drug, regimen, date, or provenance information required to reconstruct an
OMOP source fact.  Instead, it reruns the normal OMOP -> PatientRecord
derivation for records which need their therapy IDs refreshed.
"""

from django.core.management.base import BaseCommand

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import refresh_patient_record


class Command(BaseCommand):
    help = 'Re-derive PatientRecord therapy concept IDs from OMOP treatment facts'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report the PatientRecords that would be re-derived without writing',
        )

    def handle(self, *args, **options):
        records = PatientRecord.objects.select_related('person').order_by('person_id')
        total = records.count()
        if options['dry_run']:
            self.stdout.write(
                f'[DRY RUN] Would re-derive {total} PatientRecord(s) from OMOP treatment facts.'
            )
            return

        refreshed = 0
        for record in records.iterator():
            refresh_patient_record(record.person)
            refreshed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f'Done — re-derived={refreshed} PatientRecord(s) from OMOP treatment facts.'
            )
        )
