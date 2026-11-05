"""Point seeded field mappings at their Concepts on an instance that loaded Athena.

``load_athena_vocabularies`` runs this resolution itself after a successful
load. This command is for an instance that loaded its vocabulary before the
loader did so, or whose mappings were seeded or copied after the load: it
finds every ``FieldConceptMapping`` that names a concept but has no
``concept`` FK and saves the loaded Concept onto it, as a re-save in the
editor would, so the post-save projection backfills any user edit that was
pending on the field. Rows whose code is not loaded here (a vocabulary
outside the load scope, a HealthKey concept this instance has not minted)
are listed and left as they are; the command exits 0 either way, because an
unresolved row is a fact about the instance, not a failure of the command.
Re-running it is safe: a resolved row is never a candidate again.

Writes to ``DATABASE_URL``; name the target explicitly. ``--dry-run`` reports
what a run would do without locking, saving or projecting anything.
"""
from django.core.management.base import BaseCommand

from omop_core.services.field_concept_resolution import resolve_field_concept_mappings


class Command(BaseCommand):
    help = ('Point every FieldConceptMapping that names a loaded concept by '
            '(vocabulary_id, concept_code) at that Concept')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true', dest='dry_run',
            help='Report what would be resolved; write nothing.',
        )

    def handle(self, *args, **options):
        result = resolve_field_concept_mappings(dry_run=options['dry_run'])

        if result.unresolved:
            self.stdout.write(self.style.WARNING(f'  ! {result.summary()}'))
        else:
            self.stdout.write(f'  {result.summary()}')
        if options['dry_run']:
            self.stdout.write(self.style.WARNING(
                f'Dry run, nothing written. Would resolve {result.resolved} row(s).'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'Resolved {result.resolved} field mapping concept(s); '
                f'{result.unresolved} still unresolved.'
            ))
