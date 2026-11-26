"""Report HemOnc regimen ids in the LOT resolver tables that this vocabulary cannot serve (#2764).

Read-only. For each id in ``lot_regimens`` that is missing, retired, not
standard, unnamed, or not a HemOnc Regimen, prints the id, the table entries
that return it, the failed condition, and any live 'Maps to' target. Exits 1
when any are found, on the pattern of audit_retired_mapping_destinations, so a
release checklist can gate on it.

A missing id means the vocabulary install is incomplete; a wrong or retired one
means the table needs a new id. Either way, patient records derived since then
keep the old answer until they are re-derived with
``backfill_patient_records --all``.
"""
from django.core.management.base import BaseCommand

from omop_core.services.regimen_vocabulary_audit import (
    declared_regimen_targets,
    regimen_target_problems,
)


class Command(BaseCommand):
    help = 'List HemOnc regimen ids in the LOT resolver tables that the installed vocabulary cannot serve'

    def handle(self, *args, **options):
        checked = len(declared_regimen_targets())
        problems = regimen_target_problems()
        if not problems:
            self.stdout.write(self.style.SUCCESS(
                f'All {checked} regimen ids in the resolver tables are live, standard, '
                'named HemOnc Regimens.'
            ))
            return

        for problem in problems:
            line = f'{problem.concept_id} {problem.reason}; returned by {", ".join(problem.sources)}'
            if problem.maps_to:
                line += '; maps to ' + ', '.join(f'{cid} "{name}"' for cid, name in problem.maps_to)
            self.stdout.write(line)
        self.stdout.write(self.style.WARNING(
            f'{len(problems)} of {checked} regimen ids cannot be served by this vocabulary.'
        ))
        raise SystemExit(1)
