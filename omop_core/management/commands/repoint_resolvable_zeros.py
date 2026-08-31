"""Move clinical rows off concept 0 where their source code resolves today (#846).

8,708 rows in staging sit at ``*_concept_id = 0`` for codes that are in the
loaded vocabulary right now -- LOINC ``38483-4`` (Creatinine [Mass/volume] in
Blood) alone accounts for 5,737 of them. They were written before Athena was
loaded and nothing revisited them, so every derivation since has read a
perfectly ordinary creatinine result as unmapped.

These need a re-point, not a curation queue entry. ``resolve_source_code``'s
rule 1 says a LOINC or SNOMED code *is* its own concept, so proposing a mapping
for one would put exactly the codes that rule exists to keep out of the queue
into it. The sibling command ``propose_mappings_from_unresolved`` recognises
them and says so ("already resolvable: rows need re-pointing, not a proposal");
this command is the other half of that sentence.

The walk is the same: the five clinical tables at concept 0, grouped by
``(source_value, table)``. For each distinct source value it asks
``_direct_concept`` whether Athena knows the code today, and where it does, the
rows move onto that concept via ``services/code_mapping.repoint_clinical_rows``
-- the one path responsible for moving clinical data, so the collapse and
stale-marking rules cannot drift between two implementations.

Two things that path already settles, and that this command therefore inherits:

* **Collapse is on the full event identity**, never ``(person, date)``. A
  patient legitimately has several distinct results for one analyte on one day,
  and the re-point can make two rows identical only if they were the same fact.
* **Nothing is re-derived inline.** Affected ``PatientRecord`` rows are marked
  ``derivation_version=0`` and left for ``backfill_patient_records``. At 12-32s
  per bulk-loaded patient, re-deriving 8,708 rows' worth of patients inside a
  management command would be an absurd request.

Usage:
    python manage.py repoint_resolvable_zeros                    # dry run
    python manage.py repoint_resolvable_zeros --apply
    python manage.py repoint_resolvable_zeros --apply --table measurement
"""
import logging

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count

from omop_core.services.code_mapping import (
    CLINICAL_TABLES,
    NO_MATCHING_CONCEPT_ID,
    RepointTarget,
    _direct_concept,
    approved_mapping_for,
    normalize_omop_table,
    repoint_clinical_rows,
)

logger = logging.getLogger(__name__)

# Cap per table, matching the sibling command. Anything dropped is reported --
# a run that says nothing reads as "covered everything", and the whole point of
# this command is that unresolved rows went unnoticed for months.
DEFAULT_LIMIT = 500


class Command(BaseCommand):
    help = 'Re-point clinical rows at concept 0 whose source code resolves today (#846).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Write the re-point. Without this the command only reports.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Explicitly request a dry run (the default).',
        )
        parser.add_argument(
            '--table', choices=sorted(CLINICAL_TABLES), action='append',
            help='Limit to one clinical table (repeatable). Default: all five.',
        )
        parser.add_argument(
            '--limit', type=int, default=DEFAULT_LIMIT,
            help=f'Max distinct source values per table (default {DEFAULT_LIMIT}).',
        )

    def handle(self, *args, **options):
        apply_changes = options['apply']
        if apply_changes and options['dry_run']:
            raise CommandError('--apply and --dry-run are mutually exclusive.')
        tables = options['table'] or sorted(CLINICAL_TABLES)
        limit = options['limit']
        if limit < 1:
            raise CommandError('--limit must be at least 1.')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'DRY RUN — nothing will be written. Re-run with --apply.'))

        totals = {'candidates': 0, 'resolved': 0, 'unresolved': 0,
                  'rows': 0, 'collapsed': 0, 'truncated': []}
        # Union, not a sum of each sweep's count: a person with rows under two
        # source values would otherwise be reported twice.
        person_ids = set()

        for table in tables:
            person_ids |= self._sweep_table(table, limit, apply_changes, totals)

        self._summarise(totals, len(person_ids), apply_changes)

    # ------------------------------------------------------------------

    def _sweep_table(self, table, limit, apply_changes, totals):
        model, concept_col, source_col = CLINICAL_TABLES[table]
        groups = (
            model.objects
            .filter(**{concept_col: NO_MATCHING_CONCEPT_ID})
            .exclude(**{f'{source_col}__isnull': True})
            .exclude(**{source_col: ''})
            .values(source_col)
            .annotate(n=Count(concept_col))
            .order_by('-n', source_col)
        )
        # limit + 1 answers "was there more?" without a second aggregate.
        rows = list(groups[:limit + 1])
        capped = False
        if len(rows) > limit:
            rows = rows[:limit]
            # Only the count of distinct source values beyond the cap is known
            # cheaply; the rows behind them are not, and claiming a row count
            # this run never measured would be worse than saying so plainly.
            capped = True
            totals['truncated'].append(table)

        self.stdout.write(
            f'\n{table}: {len(rows)} distinct source value(s) at concept 0'
            + (f' (capped at {limit}; more remain)' if capped else ''))

        affected = set()
        unresolved = 0
        for entry in rows:
            source_value = entry[source_col]
            totals['candidates'] += 1
            concept = self._destination(source_value, table)
            if concept is None:
                unresolved += 1
                totals['unresolved'] += 1
                continue

            outcome = repoint_clinical_rows(
                # Not a SourceCodeConceptMapping: these codes resolve against
                # Athena and so need no mapping row (rule 1). See RepointTarget.
                mapping=RepointTarget(source_code=source_value, omop_table=table),
                old_concept_id=NO_MATCHING_CONCEPT_ID,
                new_concept_id=concept.concept_id,
                apply_changes=apply_changes,
                # A concept-0 sweep matches on the code alone. RepointTarget
                # carries no description anyway, but saying so keeps the intent
                # visible next to the call rather than in the default.
                match_description=False,
            )
            affected |= outcome['person_ids']
            totals['resolved'] += 1
            totals['rows'] += outcome['rows_updated']
            totals['collapsed'] += outcome['rows_collapsed']

            collapsed = outcome['rows_collapsed']
            self.stdout.write(
                f'  {outcome["rows_updated"]:>6} row(s)  {source_value[:60]:<62s}'
                f'-> {concept.vocabulary_id} {concept.concept_id} '
                f'({concept.concept_name[:40]})'
                + (f'  [{collapsed} collapsed]' if collapsed else ''))
            if apply_changes:
                logger.info(
                    'repoint_resolvable_zeros: %s %s row(s) %r -> concept %s '
                    '(%s collapsed)',
                    outcome['rows_updated'], table, source_value,
                    concept.concept_id, collapsed)

        if unresolved:
            # Not listed one by one: on a real database this is hundreds of
            # free-text lab names, and enumerating them is the other command's
            # job. The count is still shown, so the silence is never mistaken
            # for "nothing left here".
            self.stdout.write(
                f'  {unresolved} source value(s) do not resolve — left at concept 0')
        return affected

    def _destination(self, source_value, table):
        """The concept these rows should hold, or None to leave them alone.

        An approved mapping is a curator's deliberate decision and outranks a
        direct Athena hit (rule 2) -- but only for the table it was curated
        against. The same string can appear as a source value in two tables,
        and honouring a measurement mapping over drug_exposure rows would move
        them onto a concept in the wrong domain.
        """
        approved = approved_mapping_for('', source_value)
        if approved is not None and normalize_omop_table(approved.omop_table) == table:
            return approved.target_concept
        return _direct_concept('', source_value)

    def _summarise(self, totals, persons, apply_changes):
        self.stdout.write('')
        verb = 'Would re-point' if not apply_changes else 'Re-pointed'
        self.stdout.write(self.style.SUCCESS(
            f'{verb} {totals["rows"]} row(s) across {totals["resolved"]} source '
            f'value(s) of {totals["candidates"]} at concept 0  |  '
            f'{totals["collapsed"]} collapsed as duplicates  |  '
            f'{persons} PatientRecord(s) marked stale'))
        if totals['unresolved']:
            self.stdout.write(
                f'{totals["unresolved"]} source value(s) still do not resolve and were '
                f'left at concept 0 — run `manage.py propose_mappings_from_unresolved` '
                f'to queue them for curation.')
        if totals['truncated']:
            self.stdout.write(self.style.WARNING(
                f'--limit reached in: {", ".join(totals["truncated"])}. Source values '
                f'beyond the cap were NOT examined and their rows are untouched; '
                f're-run to continue.'))
        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                'Nothing was written. Re-run with --apply.'))
        elif totals['rows']:
            self.stdout.write(self.style.WARNING(
                'Run `manage.py backfill_patient_records` to re-derive the '
                'affected records.'))
