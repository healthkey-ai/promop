"""Auto-approve ICD10 (HT-One) mappings that overlap with approved ICD10CM
(Athena) mappings.

For each ICD10 ``proposed`` row whose ``source_code`` matches an ICD10CM
``approved`` row, copy the ``target_concept`` and mark it approved.  This is a
one-time catch-up: after the ICD-10-CM and ICD-10 tabs are merged, new codes
go through the normal curation flow.

Safe to re-run: already-approved ICD10 rows are skipped.

**Note:** This command does NOT call ``repoint_clinical_rows``.  After running
it, trigger a full patient-record refresh for affected persons so that stored
clinical rows pick up the newly approved concepts::

    python manage.py backfill_patient_records

Usage::

    # Preview what would change
    python manage.py apply_icd10cm_mappings --dry-run

    # Apply
    python manage.py apply_icd10cm_mappings
"""
import logging

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from omop_core.models import SourceCodeConceptMapping

logger = logging.getLogger(__name__)

_ORIGIN_FIELD = SourceCodeConceptMapping._meta.get_field('origin_system')
_ORIGIN_MAX_LEN = _ORIGIN_FIELD.max_length or 50

Identity = None  # resolved lazily to avoid import-time model registry issues


def _get_system_reviewer():
    """Get or create the 'system' Identity used for automated approvals."""
    global Identity  # noqa: PLW0603
    if Identity is None:
        from django.apps import apps
        Identity = apps.get_model(settings.AUTH_USER_MODEL)
    reviewer, _ = Identity.objects.get_or_create(
        issuer='system', sub='system',
        defaults={'uid': 'system:system', 'name': 'system'},
    )
    return reviewer


class Command(BaseCommand):
    help = 'Auto-approve ICD10 mappings that overlap with approved ICD10CM mappings.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']

        # Build lookup: source_code → approved ICD10CM row.
        # The unique constraint (source_vocabulary_id, source_code) prevents
        # duplicates at the DB level, so no dedup needed here.
        approved_icd10cm = {
            row.source_code: row
            for row in SourceCodeConceptMapping.objects.filter(
                source_vocabulary_id='ICD10CM',
                status='approved',
                target_concept__isnull=False,
            ).select_related('target_concept')
        }
        self.stdout.write(f'Approved ICD10CM mappings: {len(approved_icd10cm)}')

        # Find proposed ICD10 rows whose source_code overlaps
        pending_icd10 = SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='ICD10',
            status='proposed',
            source_code__in=approved_icd10cm.keys(),
        )
        count = pending_icd10.count()
        self.stdout.write(f'ICD10 proposed rows with ICD10CM overlap: {count}')

        if count == 0:
            self.stdout.write('Nothing to do.')
            return

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f'[DRY RUN] Would approve {count} ICD10 rows. '
                'Re-run without --dry-run to apply.'
            ))
            return

        now = timezone.now()
        reviewer = _get_system_reviewer()
        rows_to_update = []
        for row in pending_icd10.iterator():
            donor = approved_icd10cm[row.source_code]
            row.target_concept = donor.target_concept
            row.destination_vocabulary_id = donor.destination_vocabulary_id
            row.status = 'approved'
            row.reviewed_at = now
            row.reviewer = reviewer
            new_origin = (
                f'{row.origin_system}; auto-approved from ICD10CM'
                if row.origin_system
                else 'auto-approved from ICD10CM'
            )
            row.origin_system = new_origin[:_ORIGIN_MAX_LEN]
            rows_to_update.append(row)

        update_fields = [
            'target_concept', 'destination_vocabulary_id',
            'status', 'reviewed_at', 'reviewer', 'origin_system',
        ]
        with transaction.atomic():
            SourceCodeConceptMapping.objects.bulk_update(
                rows_to_update, update_fields, batch_size=500,
            )

        self.stdout.write(self.style.SUCCESS(
            f'Approved {len(rows_to_update)} ICD10 rows from ICD10CM mappings.'
        ))
