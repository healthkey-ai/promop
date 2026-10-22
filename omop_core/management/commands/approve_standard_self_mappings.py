"""Approve LOINC/SNOMED self-mappings where source_code == target concept_code.

These are standard-to-standard identity mappings that don't need curator
review — the source code already IS the standard concept.

Idempotent: only touches rows with status='proposed'.
"""

import logging

from django.core.management.base import BaseCommand
from django.db.models import F

from omop_core.models import SourceCodeConceptMapping

logger = logging.getLogger(__name__)

STANDARD_VOCABULARIES = ('LOINC', 'SNOMED')


class Command(BaseCommand):
    help = 'Approve LOINC/SNOMED proposed mappings where source_code == target concept_code.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be approved without writing.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']

        qs = SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id__in=STANDARD_VOCABULARIES,
            status='proposed',
            target_concept__isnull=False,
            source_code=F('target_concept__concept_code'),
        )

        count = qs.count()

        if dry_run:
            for vocab in STANDARD_VOCABULARIES:
                n = qs.filter(source_vocabulary_id=vocab).count()
                self.stdout.write(f'  {vocab}: {n} proposed self-mappings')
            self.stdout.write(self.style.WARNING(
                f'DRY RUN: {count} rows would be approved.'
            ))
            return

        updated = qs.update(status='approved')
        self.stdout.write(self.style.SUCCESS(
            f'Approved {updated} standard self-mappings '
            f'(LOINC + SNOMED where source_code == target concept_code).'
        ))
