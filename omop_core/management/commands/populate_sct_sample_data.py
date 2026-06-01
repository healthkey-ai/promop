"""
Management command: populate_sct_sample_data

Seeds random SCT-related values onto all Multiple Myeloma PatientInfo records
that currently have no SCT data.

Usage:
    DATABASE_URL="..." python manage.py populate_sct_sample_data [--overwrite]
"""
import random
from datetime import date, timedelta

from django.core.management.base import BaseCommand

from omop_core.models import PatientInfo

SCT_TYPES = ['autologous SCT', 'allogeneic SCT', 'tandem SCT']

SCT_ELIGIBILITY_VALUES = [
    'eligible for autologous SCT',
    'eligible for allogeneic SCT',
    'ineligible for autologous SCT',
    'ineligible for allogeneic SCT',
]


def _random_sct_date():
    """Random date between 1 and 10 years ago."""
    days_ago = random.randint(365, 365 * 10)
    return date.today() - timedelta(days=days_ago)


def _random_eligibility():
    """Pick 1–2 eligibility values (no contradictory pairs)."""
    auto = random.choice(['eligible for autologous SCT', 'ineligible for autologous SCT'])
    allo = random.choice(['eligible for allogeneic SCT', 'ineligible for allogeneic SCT'])
    if random.random() < 0.5:
        return [auto]
    return [auto, allo]


class Command(BaseCommand):
    help = 'Seed random SCT data onto Multiple Myeloma PatientInfo records'

    def add_arguments(self, parser):
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing SCT values (default: skip records that already have data)',
        )

    def handle(self, *args, **options):
        overwrite = options['overwrite']

        qs = PatientInfo.objects.filter(disease='Multiple Myeloma')
        if not overwrite:
            qs = qs.filter(stem_cell_transplant_history__isnull=True)

        total = qs.count()
        if total == 0:
            self.stdout.write('No eligible MM PatientInfo records found.')
            return

        updated = 0
        for pi in qs.iterator():
            has_sct = random.random() < 0.70  # 70% of MM patients have prior SCT

            if has_sct:
                sct_types = [random.choice(SCT_TYPES)]
                # ~20% chance of tandem adds a second type
                if sct_types[0] == 'tandem SCT' and random.random() < 0.3:
                    sct_types = ['autologous SCT', 'tandem SCT']
                pi.stem_cell_transplant_history = sct_types
                pi.sct_date = _random_sct_date()
            else:
                pi.stem_cell_transplant_history = []
                pi.sct_date = None

            pi.sct_eligibility = _random_eligibility()
            pi.save(update_fields=['stem_cell_transplant_history', 'sct_date', 'sct_eligibility'])
            updated += 1

        self.stdout.write(
            self.style.SUCCESS(f'Updated {updated}/{total} Multiple Myeloma PatientInfo records.')
        )
