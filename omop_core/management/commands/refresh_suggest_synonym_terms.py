"""Rebuild suggest_synonym_term, the domain-scoped synonym table Suggest reads.

``load_athena_vocabularies`` runs this after every full load; this command is
for an instance whose vocabulary was loaded before the table's shape changed --
the semicolon-joined LOINC names were split into one term each in #2355 -- and
needs the table brought in step without a vocabulary load. Incremental and
idempotent; writes to ``DATABASE_URL``.
"""
from django.core.management.base import BaseCommand

from omop_core.services import suggest_synonym_terms


class Command(BaseCommand):
    help = 'Bring suggest_synonym_term in step with concept and concept_synonym.'

    def handle(self, *args, **options):
        inserted, deleted = suggest_synonym_terms.refresh()
        self.stdout.write(
            f'suggest_synonym_term: {inserted} added, {deleted} removed.'
        )
