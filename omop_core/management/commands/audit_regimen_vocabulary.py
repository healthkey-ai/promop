"""Report HemOnc regimen ids the LOT resolvers return that this vocabulary cannot serve (#2764).

Read-only. Checks each id in ``MYELOMA_REGIMEN_CONCEPT_IDS``,
``REGIMEN_CONCEPT_IDS`` and ``_GENERATOR_ALIASES``. For each one that is not an
integer, missing, retired, not standard, unnamed, not a HemOnc Regimen, or
without the component edges the derivation reads, prints the id, the table
entries that return it, the failed condition, and any live successor that would
pass. Exits 1 when any are found, on the pattern of
audit_retired_mapping_destinations, so a release checklist can gate on it.

A missing id means the vocabulary install is incomplete; a retired or wrong-kind
id means the table needs a new id. An unnamed id, or one without component
edges, points at the vocabulary rows or at the relationship types the derivation
reads. Fixing any of them changes only what the resolvers return from then on.
Ids already written into DrugExposure or Episode rows (for example by the
enrichment commands) stay until those rows are corrected, and patient records
keep the old answer until they are re-derived. Re-derive on staging, never
production:
``DATABASE_URL="${STAGING_DATABASE_URL:?}" python manage.py backfill_patient_records --all``.
"""
from django.core.management.base import BaseCommand

from omop_core.services.regimen_vocabulary_audit import (
    declared_regimen_targets,
    regimen_target_problems,
)


class Command(BaseCommand):
    help = 'List HemOnc regimen ids the LOT resolvers return that the installed vocabulary cannot serve'

    def handle(self, *args, **options):
        checked = len(declared_regimen_targets())
        problems = regimen_target_problems()
        if not problems:
            self.stdout.write(self.style.SUCCESS(
                f'All {checked} regimen ids in the resolver tables are live, standard, '
                'named HemOnc Regimens with component edges.'
            ))
            return

        for problem in problems:
            line = f'{problem.concept_id!r} {problem.reason}; returned by {", ".join(problem.sources)}'
            if problem.replacements:
                line += '; live successor ' + ', '.join(f'{cid} "{name}"' for cid, name in problem.replacements)
            self.stdout.write(line)
        self.stdout.write(self.style.WARNING(f'{len(problems)} regimen id problem(s) found.'))
        raise SystemExit(1)
