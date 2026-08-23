"""Inventory legacy PatientRecord values that lack OMOP source facts."""

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from omop_core.models import PatientRecord
from omop_core.services.projection_reconciliation import (
    RECONCILABLE_FIELDS,
    migrate_candidates,
    projection_only_candidates,
)


class Command(BaseCommand):
    help = (
        "Inventory legacy PatientRecord clinical values lacking OMOP facts; "
        "writes require an explicitly attested event date."
    )

    def add_arguments(self, parser):
        parser.add_argument("--person-id", type=int, help="Limit the report to one person.")
        parser.add_argument(
            "--field",
            choices=sorted(RECONCILABLE_FIELDS),
            help=(
                "One mapped PatientRecord field. Required with --apply so a repair "
                "can never migrate a whole person or database."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Create reconcilable Measurements (requires --event-date).",
        )
        parser.add_argument(
            "--event-date",
            type=date.fromisoformat,
            help="Clinically attested YYYY-MM-DD for every created Measurement; never defaults to today.",
        )

    def handle(self, **options):
        if options["apply"] and options["event_date"] is None:
            raise CommandError("--apply requires --event-date; do not invent a clinical event date.")
        if options["apply"] and options["person_id"] is None:
            raise CommandError("--apply requires --person-id; whole-database migration is prohibited.")
        if options["apply"] and options["field"] is None:
            raise CommandError("--apply requires --field; whole-person migration is prohibited.")
        if options["event_date"] and not options["apply"]:
            raise CommandError("--event-date is only valid with --apply.")

        records = PatientRecord.objects.all()
        if options["person_id"] is not None:
            records = records.filter(person_id=options["person_id"])

        candidates = list(projection_only_candidates(records))
        if options["field"] is not None:
            candidates = [candidate for candidate in candidates if candidate.field == options["field"]]
        for candidate in candidates:
            self.stdout.write(
                f"RECONCILABLE person_id={candidate.person_id} field={candidate.field} "
                f"value={candidate.value} loinc={candidate.loinc_code} unit={candidate.unit}"
            )
        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Inventory only: {len(candidates)} mapped-field candidate(s). "
                    "No OMOP facts written. Unmapped projection-owned fields are excluded."
                )
            )
            return

        if len(candidates) != 1:
            raise CommandError(
                "The supplied --person-id and --field must identify exactly one "
                "projection-only mapped value; no migration was performed."
            )

        with transaction.atomic():
            migrated, skipped = migrate_candidates(candidates, event_date=options["event_date"])
        for candidate in skipped:
            self.stdout.write(
                self.style.WARNING(
                    f"SKIPPED person_id={candidate.person_id} field={candidate.field} "
                    "(required LOINC or Lab type concept unavailable)"
                )
            )
        self.stdout.write(self.style.SUCCESS(f"Migrated {migrated} OMOP Measurement(s)."))
