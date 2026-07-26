"""
Prune (and optionally archive) old AuditEvent rows — HL7 PHR-S FM TI.2.2.

The `audit_event` table grows by one row per audited API/OAuth request and would
otherwise grow unbounded. This command enforces a retention window: rows whose
`timestamp` is strictly older than `now - retention_days` are eligible for
deletion. Newer rows are NEVER touched.

Retention window (in days) resolution order:
  1. `--days N` (per-run override)
  2. settings.AUDIT_EVENT_RETENTION_DAYS (env: AUDIT_EVENT_RETENTION_DAYS)

Deletion happens in PK-ordered batches (`--batch-size`, default 1000) to avoid a
single huge DELETE holding a long lock on the table. When `--archive <path>` is
given, each batch is appended to the file as JSON Lines (one JSON object per row,
ISO-8601 timestamp) BEFORE it is deleted, so nothing is lost.

Usage:
    python manage.py prune_audit_events --dry-run
    python manage.py prune_audit_events --days 365
    python manage.py prune_audit_events --archive /path/to/audit-archive.jsonl
    python manage.py prune_audit_events --batch-size 5000
"""

import json

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from patient_portal.models import AuditEvent


# Columns serialized to the archive (one JSON object per row). Kept explicit so
# the archive format is stable and independent of model-field ordering.
_ARCHIVE_FIELDS = [
    'id',
    'event_type',
    'timestamp',
    'method',
    'path',
    'status_code',
    'user_id',
    'user_email',
    'client_id',
    'resource_id',
    'ip_address',
    'duration_ms',
    'detail',
]


class Command(BaseCommand):
    help = "Delete (and optionally archive) AuditEvent rows older than the retention window (TI.2.2)."

    def add_arguments(self, parser):
        parser.add_argument(
            '--days',
            type=int,
            default=None,
            help=(
                'Retention window in days for this run. Rows older than '
                'now - days are pruned. Defaults to settings.AUDIT_EVENT_RETENTION_DAYS.'
            ),
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report how many rows WOULD be pruned without deleting anything.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='Number of rows to delete per batch (default 1000).',
        )
        parser.add_argument(
            '--archive',
            type=str,
            default=None,
            help=(
                'Path to a JSON Lines file. Each batch is appended here (one JSON '
                'object per row) BEFORE it is deleted, so nothing is lost.'
            ),
        )

    def handle(self, *args, **options):
        days = options['days']
        if days is None:
            days = getattr(settings, 'AUDIT_EVENT_RETENTION_DAYS', 2190)
        if days < 0:
            raise CommandError('--days must be a non-negative integer.')

        batch_size = options['batch_size']
        if batch_size < 1:
            raise CommandError('--batch-size must be a positive integer.')

        dry_run = options['dry_run']
        archive_path = options['archive']

        cutoff = timezone.now() - timedelta(days=days)

        matched_qs = AuditEvent.objects.filter(timestamp__lt=cutoff)
        matched = matched_qs.count()

        self.stdout.write(f"Retention window: {days} day(s)")
        self.stdout.write(f"Cutoff (delete rows with timestamp <): {cutoff.isoformat()}")
        self.stdout.write(f"Matched (older than cutoff): {matched}")

        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry run: {matched} row(s) would be pruned; nothing was deleted."
            ))
            return

        if matched == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to prune."))
            return

        archive_file = None
        archived = 0
        deleted = 0
        try:
            if archive_path:
                # Append mode so repeated runs accumulate into one archive file.
                archive_file = open(archive_path, 'a', encoding='utf-8')

            while True:
                # Re-filter by cutoff every batch; only ever select rows strictly
                # older than the cutoff. Order by PK for stable batching.
                batch = list(
                    AuditEvent.objects.filter(timestamp__lt=cutoff)
                    .order_by('pk')[:batch_size]
                )
                if not batch:
                    break

                if archive_file is not None:
                    for row in batch:
                        archive_file.write(json.dumps(self._serialize(row)) + '\n')
                    archive_file.flush()
                    archived += len(batch)

                batch_ids = [row.pk for row in batch]
                AuditEvent.objects.filter(pk__in=batch_ids).delete()
                deleted += len(batch_ids)
        finally:
            if archive_file is not None:
                archive_file.close()

        self.stdout.write(f"Archived: {archived}")
        self.stdout.write(f"Deleted: {deleted}")
        if archive_path:
            self.stdout.write(self.style.SUCCESS(
                f"Pruned {deleted} audit event(s); archived to {archive_path}."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(f"Pruned {deleted} audit event(s)."))

    @staticmethod
    def _serialize(row):
        """Return a JSON-serializable dict for one AuditEvent row."""
        data = {}
        for field in _ARCHIVE_FIELDS:
            value = getattr(row, field)
            if field == 'timestamp' and value is not None:
                value = value.isoformat()
            data[field] = value
        return data
