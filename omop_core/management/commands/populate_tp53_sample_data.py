"""
Management command: populate_tp53_sample_data

Seeds plausible tp53_disruption values onto PatientRecord records that currently
have tp53_disruption=None.

- ~50% of records are left as None (unknown)
- Of the remaining ~50%, True/False is assigned based on disease type and
  clinical indicators that make TP53 disruption more or less plausible.

Usage:
    DATABASE_URL="..." python manage.py populate_tp53_sample_data [--overwrite]
"""
import random

from django.core.management.base import BaseCommand

from omop_core.models import PatientRecord

# Disease-specific base prevalence of TP53 disruption (approximate real-world rates).
_DISEASE_PREVALENCE = {
    'Breast Cancer': 0.30,
    'Chronic Lymphocytic Leukemia': 0.10,
    'Multiple Myeloma': 0.10,
    'Follicular Lymphoma': 0.05,
}
_DEFAULT_PREVALENCE = 0.10


def _tp53_probability(pr):
    """Return the probability that this patient has TP53 disruption,
    adjusted by disease and clinical indicators."""
    base = _DISEASE_PREVALENCE.get(pr.disease, _DEFAULT_PREVALENCE)

    # CLL-specific boosters
    if pr.disease == 'Chronic Lymphocytic Leukemia':
        # Advanced Binet stage (B or C) correlates with TP53 disruption
        if getattr(pr, 'binet_stage', None) in ('B', 'C'):
            base += 0.15
        # BTK inhibitor refractory suggests aggressive biology
        if getattr(pr, 'btk_inhibitor_refractory', None) is True:
            base += 0.15
        # BCL2 inhibitor refractory
        if getattr(pr, 'bcl2_inhibitor_refractory', None) is True:
            base += 0.10
        # Richter transformation is strongly associated with TP53
        if getattr(pr, 'richter_transformation', None) is True:
            base += 0.25

    # Multiple Myeloma: high beta-2 microglobulin and ISS stage III
    if pr.disease == 'Multiple Myeloma':
        b2m = getattr(pr, 'serum_beta2_microglobulin_level', None)
        if b2m is not None:
            try:
                if float(b2m) > 5.5:
                    base += 0.15
            except (ValueError, TypeError):
                pass
        iss = getattr(pr, 'iss_stage', None)
        if iss and 'III' in str(iss):
            base += 0.10

    # Breast Cancer: triple-negative subtype strongly linked to TP53
    if pr.disease == 'Breast Cancer':
        er = getattr(pr, 'er_status', None)
        pr_status = getattr(pr, 'pr_status', None)
        her2 = getattr(pr, 'her2_status', None)
        if (str(er).lower() in ('negative', '-') and
                str(pr_status).lower() in ('negative', '-') and
                str(her2).lower() in ('negative', '-')):
            base += 0.30

    return min(base, 0.95)


class Command(BaseCommand):
    help = 'Seed plausible tp53_disruption values onto PatientRecord records'

    def add_arguments(self, parser):
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing tp53_disruption values (default: skip records that already have data)',
        )

    def handle(self, *args, **options):
        overwrite = options['overwrite']

        qs = PatientRecord.objects.all()
        if not overwrite:
            qs = qs.filter(tp53_disruption__isnull=True)

        total = qs.count()
        if total == 0:
            self.stdout.write('No eligible PatientRecord records found.')
            return

        _BATCH = 500
        rows_to_update = []
        set_true = 0
        set_false = 0
        left_unknown = 0

        for pr in qs.iterator(chunk_size=_BATCH):
            # ~50% chance to leave as unknown
            if random.random() < 0.50:
                pr.tp53_disruption = None
                left_unknown += 1
            else:
                prob = _tp53_probability(pr)
                if random.random() < prob:
                    pr.tp53_disruption = True
                    set_true += 1
                else:
                    pr.tp53_disruption = False
                    set_false += 1

            rows_to_update.append(pr)

            if len(rows_to_update) >= _BATCH:
                PatientRecord.objects.bulk_update(rows_to_update, ['tp53_disruption'])
                rows_to_update.clear()

        if rows_to_update:
            PatientRecord.objects.bulk_update(rows_to_update, ['tp53_disruption'])

        self.stdout.write(self.style.SUCCESS(
            f'Updated {total} PatientRecord records: '
            f'{set_true} True, {set_false} False, {left_unknown} unknown (None).'
        ))
