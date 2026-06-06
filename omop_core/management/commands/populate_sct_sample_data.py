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

from omop_core.models import PatientInfo, SctEligibility, StemCellTransplant

SCT_TYPES_FALLBACK = ['autologous SCT', 'allogeneic SCT', 'tandem SCT']


def _get_sct_types():
    titles = list(StemCellTransplant.objects.values_list('title', flat=True))
    return titles if titles else SCT_TYPES_FALLBACK


def _random_sct_date():
    """Random date between 1 and 10 years ago."""
    days_ago = random.randint(365, 365 * 10)
    return date.today() - timedelta(days=days_ago)


def _random_eligibility():
    """Pick 1–2 eligibility values (no contradictory pairs)."""
    auto_options = [t for t in SctEligibility.objects.values_list('title', flat=True) if 'autologous' in t]
    allo_options = [t for t in SctEligibility.objects.values_list('title', flat=True) if 'allogeneic' in t]
    # Fallback if vocab not yet seeded
    if not auto_options:
        auto_options = ['eligible for autologous SCT', 'ineligible for autologous SCT']
    if not allo_options:
        allo_options = ['eligible for allogeneic SCT', 'ineligible for allogeneic SCT']
    auto = random.choice(auto_options)
    allo = random.choice(allo_options)
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
            # JSONField with default=list stores [] not NULL for new rows; match both.
            from django.db.models import Q
            qs = qs.filter(Q(stem_cell_transplant_history=[]) | Q(stem_cell_transplant_history__isnull=True))

        total = qs.count()
        if total == 0:
            self.stdout.write('No eligible MM PatientInfo records found.')
            return

        # Hoist vocab lookups outside the loop — they're immutable during this command run.
        sct_types_list = _get_sct_types()
        all_eligibility = list(SctEligibility.objects.values_list('title', flat=True))
        auto_options = [t for t in all_eligibility if 'autologous' in t] or [
            'eligible for autologous SCT', 'ineligible for autologous SCT'
        ]
        allo_options = [t for t in all_eligibility if 'allogeneic' in t] or [
            'eligible for allogeneic SCT', 'ineligible for allogeneic SCT'
        ]

        updated = 0
        for pi in qs.iterator():
            has_sct = random.random() < 0.70  # 70% of MM patients have prior SCT

            if has_sct:
                sct_types = [random.choice(sct_types_list)]
                # ~30% chance of tandem adds a second type
                if sct_types[0] == 'tandem SCT' and random.random() < 0.3:
                    sct_types = ['autologous SCT', 'tandem SCT']  # tandem always paired with autologous
                pi.stem_cell_transplant_history = sct_types
                pi.sct_date = _random_sct_date()
            else:
                pi.stem_cell_transplant_history = []
                pi.sct_date = None

            auto = random.choice(auto_options)
            allo = random.choice(allo_options)
            pi.sct_eligibility = [auto] if random.random() < 0.5 else [auto, allo]
            pi.save(update_fields=['stem_cell_transplant_history', 'sct_date', 'sct_eligibility'])
            updated += 1

        self.stdout.write(
            self.style.SUCCESS(f'Updated {updated}/{total} Multiple Myeloma PatientInfo records.')
        )
