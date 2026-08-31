"""Seed the Code Mapping review queue from clinical rows already imported.

Everything ingested before the resolver started recording proposals left its
unresolved codes as ``*_concept_id = 0`` with the raw text in
``*_source_value``, and nothing in the mapping table.  Without a backfill the
Unmapped tab opens empty on a database full of unmapped codes, and a curator
has to already know which code to type before they can map it.

This walks the five clinical tables for concept 0, groups by
``(source_value, table)``, and runs each through ``resolve_source_code`` -- so
each distinct unresolved code gets a minted HK-* destination and a *proposed*
mapping carrying its real occurrence count.  Ordering the queue by that count is
what lets an SME spend their time on the code seen four hundred times rather
than the one seen once.

It does not re-point the rows themselves.  Approving a mapping does that
(``services/code_mapping.repoint_clinical_rows``), which keeps one path
responsible for moving clinical data instead of two.

Usage:
    python manage.py propose_mappings_from_unresolved              # dry run
    python manage.py propose_mappings_from_unresolved --apply
    python manage.py propose_mappings_from_unresolved --apply --table measurement
"""
import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from omop_core.models import RegimenMappingGap, SourceCodeConceptMapping
from omop_core.services.code_mapping import (
    CLINICAL_TABLES,
    _direct_concept,
    approved_mapping_for,
    resolve_source_code,
)

logger = logging.getLogger(__name__)

NO_MATCHING_CONCEPT_ID = 0

# Cap per table so one pathological import cannot mint tens of thousands of
# concepts in a single unattended run. Anything dropped is reported, never
# silently truncated -- a run that says nothing reads as "covered everything".
DEFAULT_LIMIT = 500


class Command(BaseCommand):
    help = 'Propose Code Mapping rows for source values that resolved to concept 0.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the proposals. Without this the command only reports.',
        )
        parser.add_argument(
            '--table', choices=sorted(CLINICAL_TABLES), action='append',
            help='Limit to one clinical table (repeatable). Default: all five.',
        )
        parser.add_argument(
            '--limit', type=int, default=DEFAULT_LIMIT,
            help=f'Max distinct source values per table (default {DEFAULT_LIMIT}).',
        )
        parser.add_argument(
            '--source-system', default='backfill',
            help="Recorded as the proposal's origin_system.",
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        tables = options['table'] or sorted(CLINICAL_TABLES)
        limit = options['limit']

        # Already curated, in either table. RegimenMappingGap is the same queue
        # for regimen names and is unioned in rather than duplicated.
        known = {
            (m.source_vocabulary_id, (m.source_code or '').upper())
            for m in SourceCodeConceptMapping.objects.all()
        }
        known |= {
            ('', (g.source_value or '').upper())
            for g in RegimenMappingGap.objects.all()
        }

        totals = {'candidates': 0, 'proposed': 0, 'skipped_known': 0,
                  'resolvable': 0, 'truncated': 0}

        for table in tables:
            model, concept_col, source_col = CLINICAL_TABLES[table]
            groups = (
                model.objects
                .filter(**{concept_col: NO_MATCHING_CONCEPT_ID})
                .exclude(**{f'{source_col}__isnull': True})
                .exclude(**{source_col: ''})
                .values(source_col)
                .annotate(n=Count(concept_col))
                .order_by('-n')
            )
            rows = list(groups[:limit + 1])
            truncated = len(rows) > limit
            if truncated:
                rows = rows[:limit]
                totals['truncated'] += 1

            self.stdout.write(f'\n{table}: {len(rows)} distinct unresolved source value(s)'
                              + (f' (capped at {limit})' if truncated else ''))

            for entry in rows:
                source_value = entry[source_col]
                count = entry['n']
                totals['candidates'] += 1
                if ('', source_value.upper()) in known:
                    totals['skipped_known'] += 1
                    continue

                # A code that resolves now but landed at 0 earlier -- imported
                # before its vocabulary was loaded -- needs no proposal. It needs
                # its rows moved, which is a different job. Say so rather than
                # skipping in silence, or the operator reads the quiet as
                # "nothing to do here".
                existing = (approved_mapping_for('', source_value)
                            or _direct_concept('', source_value))
                if existing is not None:
                    concept = getattr(existing, 'target_concept', existing)
                    totals['resolvable'] += 1
                    self.stdout.write(
                        f'  {count:>6}x  {source_value}  '
                        f'-> already resolvable: {concept.vocabulary_id} '
                        f'{concept.concept_id}; rows need re-pointing, not a proposal'
                    )
                    continue

                self.stdout.write(f'  {count:>6}x  {source_value}')
                if not apply_changes:
                    continue

                with transaction.atomic():
                    concept, mapping = resolve_source_code(
                        source_code=source_value,
                        source_text=source_value,
                        omop_table=table,
                        source_system=options['source_system'],
                    )
                    if mapping is None:
                        # Resolved to a real concept after all, or the table has
                        # no quarantine vocabulary. Either way nothing to review.
                        continue
                    # The queue is only useful if it reflects how much each code
                    # actually matters, and resolve_source_code counts sightings
                    # one at a time.
                    mapping.occurrence_count = count
                    mapping.save(update_fields=['occurrence_count'])
                    known.add(('', source_value.upper()))
                    totals['proposed'] += 1
                    logger.info(
                        'Proposed %s -> concept %s (%s, seen %s times)',
                        source_value, concept.concept_id, table, count,
                    )

        verb = 'Would propose' if not apply_changes else 'Proposed'
        self.stdout.write('')
        # On --apply report what was actually written. Falling through to the
        # candidate count when nothing was proposed claimed rows the run never
        # created, which is the one number an operator reads.
        count = totals['proposed'] if apply_changes else (
            totals['candidates'] - totals['skipped_known'] - totals['resolvable'])
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {count} mapping(s) from {totals["candidates"]} candidate(s); '
            f'{totals["skipped_known"]} already curated, '
            f'{totals["resolvable"]} already resolvable.'
        ))
        if totals['resolvable']:
            self.stdout.write(self.style.WARNING(
                f'{totals["resolvable"]} source value(s) already resolve to a real '
                f'concept but their rows still hold concept 0 -- they were imported '
                f'before that vocabulary was loaded. Re-import or re-point those rows.'
            ))
        if totals['truncated']:
            self.stdout.write(self.style.WARNING(
                f'{totals["truncated"]} table(s) hit the --limit cap; re-run to continue.'
            ))
