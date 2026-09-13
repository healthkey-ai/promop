"""Populate durable stage facts for existing sample patients."""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from omop_core.models import PatientRecord
from omop_core.services.sample_patient_stage import SAMPLE_ORG_DISEASES, ensure_sample_patient_stage


class Command(BaseCommand):
    help = 'Backfill stage for sample cohorts; preview by default, apply with --confirm.'

    def add_arguments(self, parser):
        parser.add_argument('--org-slugs', default=','.join(SAMPLE_ORG_DISEASES),
                            help='Comma-separated sample organizations. Explicit selections are treated as synthetic data.')
        parser.add_argument('--confirm', action='store_true', help='Persist stage facts and patient stage.')
        parser.add_argument('--dry-run', action='store_true', help='Preview without writing, even with --confirm.')
        parser.add_argument('--limit', type=int, help='Process only the first N patients.')

    def handle(self, *args, **options):
        slugs = [s.strip() for s in options['org_slugs'].split(',') if s.strip()]
        if not slugs:
            raise CommandError('Select at least one sample organization with --org-slugs.')
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('--limit must be positive.')
        scope = Q()
        for slug in slugs:
            scope |= Q(organization__slug__iexact=slug)
        records = PatientRecord.objects.filter(scope).select_related('person', 'organization').order_by('pk')
        if options['limit']:
            records = records[:options['limit']]
        dry_run = options['dry_run'] or not options['confirm']
        counts = Counter()
        self.stdout.write(f'{"Preview" if dry_run else "Applying"} sample stage backfill: {", ".join(slugs)}')
        for record in records.iterator(chunk_size=200):
            with transaction.atomic():
                stage, source = ensure_sample_patient_stage(record, dry_run=dry_run)
                counts[source] += 1
                if stage and stage != record.stage:
                    counts['updated'] += 1
                    if not dry_run:
                        PatientRecord.objects.filter(pk=record.pk).update(stage=stage)
            counts['processed'] += 1
            if counts['processed'] % 100 == 0:
                self.stdout.write(f'  Processed {counts["processed"]} patients')
        self.stdout.write(', '.join(f'{key}={value}' for key, value in sorted(counts.items())))
        if counts['unsupported disease']:
            raise CommandError(f'{counts["unsupported disease"]} patients have no supported disease or stage; no stage invented for them.')
        self.stdout.write(self.style.SUCCESS('Preview complete; pass --confirm to apply.' if dry_run else 'Sample stage backfill complete.'))
