"""Point answer-mapping proposals that carry only a code at their loaded concept.

Migration 0367 carries each choice's primary coded association over as a
proposal; on an instance whose vocabularies load afterwards it has no target
until this runs. ``load_athena_vocabularies`` runs it after every load. A
proposal whose concept is a current standard one gets it as its target and is
ready to review; one that is retired or not standard is recorded as such; one
whose concept is not loaded is left as it is. Approved mappings are never
touched and a replay changes nothing.

Writes to ``DATABASE_URL``; name the target explicitly. ``--dry-run`` counts
without writing.
"""
from django.core.management.base import BaseCommand

from omop_core.services.field_value_mappings import resolve_unresolved


class Command(BaseCommand):
    help = 'Resolve answer-to-concept proposals that name a concept by vocabulary and code'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', dest='dry_run',
                            help='Count what would be resolved; write nothing.')

    def handle(self, *args, **options):
        counts = resolve_unresolved(dry_run=options['dry_run'])
        verb = 'Would resolve' if options['dry_run'] else 'Resolved'
        self.stdout.write(
            f"{verb} {counts['resolved']} proposal(s); {counts['invalid']} retired or invalid, "
            f"{counts['non_standard']} not standard, {counts['still_unresolved']} concept not loaded.")
