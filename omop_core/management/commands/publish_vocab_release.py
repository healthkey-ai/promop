"""Publish a VocabRelease manifest for the current vocabulary corpus tables.

One-shot publisher used until the Athena loader stages+publishes atomically
itself (issue #236 PR 3).  Safe to run repeatedly — each run creates a new
release; consumers pin to ``releases/latest/``.
"""
from django.core.management.base import BaseCommand

from omop_core.services.vocab_release import publish_release


class Command(BaseCommand):
    help = 'Publish a VocabRelease manifest (scope, versions, checksums) for the current vocabulary tables.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--notes', default='',
            help='Free-text notes recorded on the release (e.g. Athena bundle date).',
        )

    def handle(self, *args, **options):
        release = publish_release(notes=options['notes'])
        self.stdout.write(self.style.SUCCESS(
            f'Published {release.release_id} '
            f'({len(release.corpus_scope.get("loaded_vocabularies", []))} vocabularies)',
        ))
        for table, count in release.row_counts.items():
            checksum = release.table_checksums.get(table, '')
            self.stdout.write(f'  {table:<24} {count:>10} rows  {checksum[:12]}…')
