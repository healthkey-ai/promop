"""Recount occurrence_count on SourceCodeConceptMapping from clinical tables.

Unlike ``enqueue_unmapped_source_codes`` (which only counts ``concept_id = 0``
rows), this command counts **all** clinical rows carrying each source value,
regardless of resolution status.  It also sets ``first_seen`` / ``last_seen``
from the earliest and latest clinical dates.

Only existing global (``organization IS NULL``) mapping rows are updated —
no new rows are created.
"""

from datetime import datetime, time, timezone as dt_tz

from django.core.management.base import BaseCommand
from django.db.models import (
    Case,
    CharField,
    Count,
    F,
    Max,
    Min,
    Q,
    Value,
    When,
)

from omop_core.mapping.code_resolution import CLINICAL_TABLES, NO_MATCHING_CONCEPT_ID
from omop_core.models import SourceCodeConceptMapping

DATE_COLUMN = {
    'measurement': 'measurement_date',
    'observation': 'observation_date',
    'condition': 'condition_start_date',
    'drug_exposure': 'drug_exposure_start_date',
    'procedure': 'procedure_date',
}


def _date_to_datetime(d):
    """Convert a date to a timezone-aware datetime (start of day UTC)."""
    if d is None:
        return None
    if isinstance(d, datetime):
        return d
    return datetime.combine(d, time.min, tzinfo=dt_tz.utc)


class Command(BaseCommand):
    help = (
        'Recount occurrence_count on SourceCodeConceptMapping rows from '
        'all clinical rows (not just concept_id = 0).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Report counts but write nothing.',
        )
        parser.add_argument(
            '--table',
            action='append',
            dest='tables',
            choices=sorted(CLINICAL_TABLES),
            help='Restrict to specific table(s). May be repeated. Default: all five.',
        )
        parser.add_argument(
            '--batch-size',
            type=int,
            default=1000,
            help='bulk_update batch size (default 1000).',
        )

    def handle(self, *args, **options):
        tables = options['tables'] or sorted(CLINICAL_TABLES)
        dry_run = options['dry_run']
        batch_size = options['batch_size']

        total_updated = 0
        total_skipped = 0

        for table_name in tables:
            model, _concept_col, source_col = CLINICAL_TABLES[table_name]
            date_col = DATE_COLUMN[table_name]
            source_concept_col = source_col.replace(
                '_source_value', '_source_concept_id',
            )

            # Derive vocabulary_id from source_concept FK
            source_vocabulary = Case(
                When(
                    Q(**{f'{source_concept_col}__isnull': True})
                    | Q(**{source_concept_col: NO_MATCHING_CONCEPT_ID}),
                    then=Value(''),
                ),
                default=F(f'{source_concept_col}__vocabulary_id'),
                output_field=CharField(),
            )

            # GROUP BY (source_value, vocabulary) → count, min(date), max(date)
            aggregated = (
                model.objects
                .exclude(**{f'{source_col}__isnull': True})
                .exclude(**{source_col: ''})
                .annotate(source_vocabulary_id=source_vocabulary)
                .values(source_col, 'source_vocabulary_id')
                .annotate(
                    occurrences=Count('pk'),
                    earliest=Min(date_col),
                    latest=Max(date_col),
                )
            )

            # Build lookup of existing SCCM rows for this table (global only)
            sccm_rows = {
                (m.source_vocabulary_id, m.source_code): m
                for m in SourceCodeConceptMapping.objects.filter(
                    omop_table=table_name,
                    organization__isnull=True,
                ).only(
                    'pk', 'source_vocabulary_id', 'source_code',
                    'occurrence_count', 'first_seen', 'last_seen',
                )
            }

            table_updated = 0
            table_skipped = 0
            pending = []

            for row in aggregated:
                key = (row['source_vocabulary_id'], row[source_col])
                mapping = sccm_rows.get(key)
                if not mapping:
                    table_skipped += 1
                    continue

                mapping.occurrence_count = row['occurrences']
                mapping.first_seen = _date_to_datetime(row['earliest'])
                mapping.last_seen = _date_to_datetime(row['latest'])
                pending.append(mapping)
                table_updated += 1

                if not dry_run and len(pending) >= batch_size:
                    SourceCodeConceptMapping.objects.bulk_update(
                        pending,
                        ['occurrence_count', 'first_seen', 'last_seen'],
                        batch_size=batch_size,
                    )
                    pending = []

            if not dry_run and pending:
                SourceCodeConceptMapping.objects.bulk_update(
                    pending,
                    ['occurrence_count', 'first_seen', 'last_seen'],
                    batch_size=batch_size,
                )

            prefix = '[DRY RUN] ' if dry_run else ''
            self.stdout.write(
                f'{prefix}{table_name}: updated {table_updated}, '
                f'skipped {table_skipped} (no mapping row)'
            )
            total_updated += table_updated
            total_skipped += table_skipped

        self.stdout.write(self.style.SUCCESS(
            f'{"[DRY RUN] " if dry_run else ""}Done: '
            f'{total_updated} mapping(s) updated, '
            f'{total_skipped} source value(s) skipped.'
        ))
