"""Put clinical source codes that have no queue row at all onto the tab.

Suggest reads the Code Mapping tab and writes destinations onto the rows it
finds there (see ``omop_core.mapping.suggestions``).  A code can only be
suggested for once it is on a tab, and ingest puts it there: every unresolved
code the resolver meets gets a ``proposed`` row through ``_record_proposal``.

What is left over is the residue from before the resolver existed -- clinical
rows sitting at ``concept_id = 0`` whose source value never became a mapping.
Finding them means grouping a whole clinical table and subtracting every
existing mapping, which costs 4-7s per table on staging.  That is a batch job,
not something a Suggest click should pay for on every run, which is what it used
to be: the scan ran per request, and on the two tabs with a real backlog
(ICD10, 10,334 rows awaiting review; RxNorm, 3,856) it returned nothing at all,
because ingest had already created a row for every code it could find.

The rows this creates carry no destination and provenance ``''``, which is
exactly the state Suggest looks for.  So the split is: this enqueues, Suggest
proposes.
"""
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from omop_core.mapping.code_resolution import (
    CLINICAL_TABLES,
    SOURCE_CODE_MAX,
    _DOMAIN_FOR_TABLE,
)
from omop_core.mapping.suggestions import DEFAULT_MIN_OCCURRENCES, unmapped_source_values
from omop_core.models import SourceCodeConceptMapping


class Command(BaseCommand):
    help = 'Create empty Code Mapping queue rows for clinical codes that have none.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--table', action='append', dest='tables', choices=sorted(CLINICAL_TABLES),
            help='Clinical table to scan; repeatable. Default: all of them.',
        )
        parser.add_argument(
            '--source-vocabulary', dest='source_vocabulary_id', default=None,
            help='Only enqueue codes from this source vocabulary.',
        )
        parser.add_argument(
            '--min-occurrences', type=int, default=DEFAULT_MIN_OCCURRENCES,
            help=(
                'How often a code must appear to be worth a curator queue row. '
                f'Default {DEFAULT_MIN_OCCURRENCES}.'
            ),
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Stop after this many new rows per table.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be enqueued and write nothing.',
        )

    def handle(self, *args, **options):
        tables = options['tables'] or sorted(CLINICAL_TABLES)
        dry_run = options['dry_run']
        now = timezone.now()
        total = 0

        for table in tables:
            values = unmapped_source_values(
                table,
                min_occurrences=options['min_occurrences'],
                limit=options['limit'],
                source_vocabulary_id=options['source_vocabulary_id'],
            )
            if not values:
                self.stdout.write(f'{table}: nothing to enqueue.')
                continue

            rows = [
                SourceCodeConceptMapping(
                    source_vocabulary_id=vocabulary_id,
                    source_code=source_value[:SOURCE_CODE_MAX],
                    source_code_description='',
                    domain_id=_DOMAIN_FOR_TABLE.get(table, ''),
                    omop_table=table,
                    status='proposed',
                    origin='import',
                    # Empty on purpose: this is the provenance Suggest treats as
                    # "nobody has spoken for this row yet".
                    origin_system='',
                    occurrence_count=occurrences,
                    first_seen=now,
                    last_seen=now,
                )
                for source_value, vocabulary_id, occurrences in values
            ]

            if dry_run:
                self.stdout.write(f'{table}: would enqueue {len(rows)} row(s).')
                for row in rows[:10]:
                    self.stdout.write(
                        f'    {row.source_vocabulary_id or "(none)"}:{row.source_code} '
                        f'x{row.occurrence_count}'
                    )
                total += len(rows)
                continue

            with transaction.atomic():
                # ignore_conflicts: a concurrent ingest can create the same row
                # between the scan and the insert, and the unique constraint on
                # (source_vocabulary_id, source_code) is the right thing to let
                # win -- its row carries real ingest provenance.
                #
                # Counted with a before/after delta, not from the returned
                # instances: Django turns RETURNING off whenever on_conflict is
                # set, so every returned row has pk None and counting those
                # reports 0 after inserting thousands.
                before = SourceCodeConceptMapping.objects.count()
                SourceCodeConceptMapping.objects.bulk_create(rows, ignore_conflicts=True)
                landed = SourceCodeConceptMapping.objects.count() - before
            total += landed
            self.stdout.write(f'{table}: enqueued {landed} of {len(rows)} row(s).')

        verb = 'would enqueue' if dry_run else 'enqueued'
        self.stdout.write(self.style.SUCCESS(f'Done: {verb} {total} row(s).'))
