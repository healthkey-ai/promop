"""Backfill source descriptions, source concepts, and UMLS names for ICD-10
proposed mappings (#1043).

Existing ICD-10 (HT-One) SCCM rows were imported before the UMLS lookup was
enabled for ICD10 (#1028).  This command enriches them from UMLS and the
ICD10CM Athena vocabulary, filling in:

- ``source_code_description`` — from the UMLS preferred name (if blank)
- ``source_concept`` — from the ICD10CM Athena vocabulary (if null)
- ``umls_source_name`` — canonical UMLS preferred name (if blank)

Safe to re-run: only touches rows where at least one field is missing.

Usage::

    # Preview
    python manage.py backfill_icd10_source_info --dry-run

    # Apply
    python manage.py backfill_icd10_source_info
"""
import logging

from django.core.management.base import BaseCommand
from django.db import transaction

from omop_core.mapping.suggestions import (
    VOCAB_TO_UMLS_ROOT,
    _find_source_concept,
)
from omop_core.models import SourceCodeConceptMapping, UmlsSourceCode

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = 'Backfill source descriptions and source concepts for ICD-10 mappings from UMLS.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would change without writing.',
        )

    def handle(self, **options):
        dry_run = options['dry_run']
        umls_root = VOCAB_TO_UMLS_ROOT.get('ICD10')
        if not umls_root:
            self.stderr.write('ICD10 not in VOCAB_TO_UMLS_ROOT — nothing to do.')
            return

        # Find ICD10 rows missing at least one enrichment field.
        from django.db.models import Q
        candidates = SourceCodeConceptMapping.objects.filter(
            source_vocabulary_id='ICD10',
        ).filter(
            Q(source_code_description='')
            | Q(source_concept__isnull=True)
            | Q(umls_source_name='')
        )
        total = candidates.count()
        self.stdout.write(f'ICD10 rows needing enrichment: {total}')
        if total == 0:
            self.stdout.write('Nothing to do.')
            return

        # Pre-load UMLS preferred names for ICD10CM SAB in one query.
        umls_names = dict(
            UmlsSourceCode.objects
            .filter(root_source=umls_root, is_preferred=True)
            .values_list('code', 'name')
        )
        self.stdout.write(f'UMLS ICD10CM preferred names loaded: {len(umls_names)}')

        rows_to_update = []
        update_fields = set()
        for row in candidates.select_related('source_concept').iterator():
            changed = False

            # UMLS source name
            umls_name = umls_names.get(row.source_code, '')
            if not row.umls_source_name and umls_name:
                row.umls_source_name = umls_name
                update_fields.add('umls_source_name')
                changed = True

            # Source code description — from UMLS if blank
            if not row.source_code_description and umls_name:
                row.source_code_description = umls_name[:255]
                update_fields.add('source_code_description')
                changed = True

            # Source concept — from ICD10CM Athena vocabulary
            if row.source_concept is None:
                concept = _find_source_concept('ICD10', row.source_code)
                if concept:
                    row.source_concept = concept
                    update_fields.add('source_concept')
                    changed = True

            if changed:
                rows_to_update.append(row)

        self.stdout.write(f'Rows to update: {len(rows_to_update)}')
        if not rows_to_update:
            self.stdout.write('Nothing to update.')
            return

        if dry_run:
            # Show a sample
            for row in rows_to_update[:5]:
                self.stdout.write(
                    f'  {row.source_code}: desc={row.source_code_description!r}, '
                    f'concept={row.source_concept_id}, umls={row.umls_source_name[:60]!r}'
                )
            if len(rows_to_update) > 5:
                self.stdout.write(f'  ... and {len(rows_to_update) - 5} more')
            self.stdout.write(self.style.WARNING(
                f'[DRY RUN] Would update {len(rows_to_update)} rows. '
                'Re-run without --dry-run to apply.'
            ))
            return

        with transaction.atomic():
            SourceCodeConceptMapping.objects.bulk_update(
                rows_to_update, list(update_fields), batch_size=500,
            )

        self.stdout.write(self.style.SUCCESS(
            f'Updated {len(rows_to_update)} ICD10 rows '
            f'(fields: {", ".join(sorted(update_fields))}).'
        ))
