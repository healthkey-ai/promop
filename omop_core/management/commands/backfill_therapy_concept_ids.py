"""
backfill_therapy_concept_ids.py — Populate first_line_therapy_id, second_line_therapy_id,
and later_therapy_ids on existing PatientInfo records by re-running the regimen
concept_id lookup against the stored therapy text fields.

Usage:
    DATABASE_URL="..." python manage.py backfill_therapy_concept_ids
    DATABASE_URL="..." python manage.py backfill_therapy_concept_ids --dry-run
"""
from django.core.management.base import BaseCommand
from omop_core.models import PatientInfo, Concept
from omop_core.services.lot_regimens import MYELOMA_REGIMEN_LOOKUP, MYELOMA_REGIMEN_CONCEPT_IDS


def _text_to_concept_id(therapy_text: str) -> int | None:
    """
    Attempt to reverse-map a therapy display string to a HemOnc concept_id.
    Checks MYELOMA_REGIMEN_CONCEPT_IDS by matching the display name against
    MYELOMA_REGIMEN_LOOKUP values.
    """
    if not therapy_text:
        return None
    # Build reverse map: display_name → concept_id
    for key, name in MYELOMA_REGIMEN_LOOKUP.items():
        if name == therapy_text and key in MYELOMA_REGIMEN_CONCEPT_IDS:
            return MYELOMA_REGIMEN_CONCEPT_IDS[key]
    return None


class Command(BaseCommand):
    help = "Backfill HemOnc concept_ids on PatientInfo therapy fields"

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Print stats without writing to DB',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        updated = 0
        skipped = 0
        no_match = 0

        qs = PatientInfo.objects.filter(
            first_line_therapy__isnull=False,
        ) | PatientInfo.objects.filter(
            second_line_therapy__isnull=False,
        ) | PatientInfo.objects.filter(
            later_therapy__isnull=False,
        )

        for pi in qs.distinct():
            changed = False
            updates = {}

            if pi.first_line_therapy and pi.first_line_therapy_id is None:
                cid = _text_to_concept_id(pi.first_line_therapy)
                if cid and Concept.objects.filter(concept_id=cid).exists():
                    updates['first_line_therapy_id'] = cid
                    changed = True
                else:
                    no_match += 1

            if pi.second_line_therapy and pi.second_line_therapy_id is None:
                cid = _text_to_concept_id(pi.second_line_therapy)
                if cid and Concept.objects.filter(concept_id=cid).exists():
                    updates['second_line_therapy_id'] = cid
                    changed = True
                else:
                    no_match += 1

            if pi.later_therapy and not pi.later_therapy_ids:
                cid = _text_to_concept_id(pi.later_therapy)
                if cid and Concept.objects.filter(concept_id=cid).exists():
                    updates['later_therapy_ids'] = [cid]
                    changed = True
                else:
                    no_match += 1

            if changed:
                if not dry_run:
                    PatientInfo.objects.filter(pk=pi.pk).update(**updates)
                updated += 1
            else:
                skipped += 1

        mode = '[DRY RUN] ' if dry_run else ''
        self.stdout.write(
            f"{mode}Done — updated={updated}, skipped={skipped}, no_match={no_match}"
        )
