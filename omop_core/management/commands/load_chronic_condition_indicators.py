from django.core.management.base import BaseCommand, CommandError

from omop_core.models import ChronicConditionIndicator
from omop_core.services.chronic_conditions import CCIR_CSV, load_indicators


class Command(BaseCommand):
    help = (
        "Load AHRQ's Chronic Condition Indicator Refined (CCIR) CSV into "
        "ChronicConditionIndicator (#2512). Idempotent: the table ends up equal to the file."
    )

    def add_arguments(self, parser):
        parser.add_argument('--csv', default=str(CCIR_CSV), help='Path to a CCIR CSV (default: the bundled release)')

    def handle(self, *args, csv=None, **options):
        try:
            counts = load_indicators(ChronicConditionIndicator, csv)
        except FileNotFoundError as exc:
            raise CommandError(f'No CCIR file at {csv}') from exc
        self.stdout.write(self.style.SUCCESS(
            'CCIR loaded: {total} codes ({created} added, {updated} changed, {removed} removed).'.format(**counts)))
