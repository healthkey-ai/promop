"""Materialize validated OHDSI ARTEMIS LOT output into OMOP Episodes."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from omop_core.services.artemis_adapter import materialize_artemis_output, validate_artemis_output


class Command(BaseCommand):
    help = (
        "Validate a versioned ARTEMIS result JSON and materialize its therapy "
        "Episodes/EpisodeEvents. Does not run R or ARTEMIS itself."
    )

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Path to ARTEMIS result JSON")
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing")

    def handle(self, *args, **options):
        input_path = Path(options["input"])
        try:
            with input_path.open(encoding="utf-8") as stream:
                payload = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Could not read ARTEMIS JSON: {exc}") from exc

        try:
            records = validate_artemis_output(payload)
            if options["dry_run"]:
                self.stdout.write(f"[DRY RUN] Validated {len(records)} ARTEMIS episode(s); no rows written.")
                return
            result = materialize_artemis_output(payload)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS(
            "Done — created: {created}, updated: {updated}, skipped manual: {skipped}.".format(
                created=result.created, updated=result.updated, skipped=result.skipped_manual,
            )
        ))
