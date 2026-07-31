"""
Re-derive PatientRecord rows whose derivation_version is stale.

Usage:
    # Re-derive only records older than the current DERIVATION_VERSION
    python manage.py backfill_patient_records

    # Re-derive records older than a specific version
    python manage.py backfill_patient_records --version 3

    # Re-derive every record regardless of version
    python manage.py backfill_patient_records --all

    # Preview without modifying
    python manage.py backfill_patient_records --dry-run

    # Control batch size (default 100)
    python manage.py backfill_patient_records --batch-size 50
"""

import logging

from django.core.management.base import BaseCommand

from omop_core.models import PatientRecord
from omop_core.services.patient_record_service import (
    DERIVATION_VERSION,
    refresh_patient_record,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-derive PatientRecord rows whose derivation_version is below the current version."

    def add_arguments(self, parser):
        parser.add_argument(
            "--target-version",
            type=int,
            default=None,
            help=(
                "Target version — re-derive records with derivation_version < this value. "
                f"Defaults to current DERIVATION_VERSION ({DERIVATION_VERSION})."
            ),
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="backfill_all",
            help="Re-derive every record regardless of its current version.",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of records to process per batch (default 100).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many records would be re-derived without modifying any.",
        )

    def handle(self, **options):
        target = options["target_version"] or DERIVATION_VERSION
        backfill_all = options["backfill_all"]
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]

        if backfill_all:
            qs = PatientRecord.objects.select_related("person").all()
            label = "all"
        else:
            qs = PatientRecord.objects.select_related("person").filter(
                derivation_version__lt=target,
            )
            label = f"derivation_version < {target}"

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS("No stale records found."))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[dry-run] {total} record(s) matching {label} would be re-derived."
                )
            )
            return

        self.stdout.write(f"Re-deriving {total} record(s) matching {label} ...")

        success = 0
        errors = 0
        # Use iterator to avoid loading all records into memory at once.
        for record in qs.iterator(chunk_size=batch_size):
            try:
                refresh_patient_record(record.person)
                success += 1
                if success % batch_size == 0:
                    self.stdout.write(f"  ... {success}/{total}")
            except Exception:
                errors += 1
                logger.exception(
                    "Failed to refresh PatientRecord for person_id=%s",
                    record.person_id,
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Done. {success} re-derived, {errors} error(s)."
            )
        )
