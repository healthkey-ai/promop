"""Copy reference data from another PRomop instance into this one.

Reference data is the domain knowledge needed to import patients: field and code
mappings, lookup lists, therapy reference data and HealthKey-minted concepts.
See omop_core/services/instance_data.py for the full classification.

Usage::

    SOURCE_DATABASE_URL="postgresql://..." manage.py copy_reference_data --dry-run
    SOURCE_DATABASE_URL="postgresql://..." manage.py copy_reference_data

The destination is DATABASE_URL. The source is opened read-only. Rows are
matched on natural keys and overwritten from the source, in one transaction.
Vocabularies, UMLS, LOINC classes and survey definitions come from their own
release loaders and are not copied.
"""
import os
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connections

from omop_core.models import VocabularyRelease
from omop_core.services.instance_copy import SOURCE_ALIAS, register_source_connection
from omop_core.services.instance_data import REFERENCE_FROM_RELEASE
from omop_core.services.reference_transfer import apply_reference, read_reference


class Command(BaseCommand):
    help: str = 'Copy reference data from the instance at SOURCE_DATABASE_URL into this one.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument('--source-url', help='Source database URL. Defaults to $SOURCE_DATABASE_URL.')
        parser.add_argument(
            '--prune', action='store_true',
            help='Also delete field and code mappings the source does not have.',
        )
        parser.add_argument('--dry-run', action='store_true', help='Report what would change and roll back.')

    def handle(self, **options: Any) -> None:
        url = options.get('source_url') or os.environ.get('SOURCE_DATABASE_URL')
        if not url:
            raise CommandError('Set SOURCE_DATABASE_URL (or pass --source-url) to the source database.')

        register_source_connection(url)
        try:
            self._warn_on_release_mismatch()
            payload = read_reference(SOURCE_ALIAS, stream=True)
            stats = apply_reference(payload, prune=options['prune'], dry_run=options['dry_run'])
        except CommandError:
            raise
        except Exception as exc:
            # Streamed tables are read while writing, so a source failure can surface here.
            raise CommandError(f'Could not copy from the source database: {exc}')
        finally:
            connections[SOURCE_ALIAS].close()

        for warning in stats.warnings:
            self.stdout.write(self.style.WARNING(f'  ! {warning}'))
        if stats.suppressed_warnings:
            self.stdout.write(self.style.WARNING(f'  ! ...and {stats.suppressed_warnings} more warnings.'))

        tables = sorted({*stats.created, *stats.updated, *stats.deleted, *stats.skipped})
        for table in tables:
            self.stdout.write(
                f'  {table:34s} created {stats.created.get(table, 0):6d}  '
                f'updated {stats.updated.get(table, 0):6d}  '
                f'deleted {stats.deleted.get(table, 0):6d}  skipped {stats.skipped.get(table, 0):6d}'
            )
        loaders = sorted(set(REFERENCE_FROM_RELEASE.values()))
        self.stdout.write(f'  Not copied, load from releases: {", ".join(loaders)}.')

        summary = (
            f'{stats.total(stats.created)} created, {stats.total(stats.updated)} updated, '
            f'{stats.total(stats.deleted)} deleted'
        )
        if options['dry_run']:
            self.stdout.write(self.style.WARNING(f'Dry run, rolled back. Would have been: {summary}.'))
        else:
            self.stdout.write(self.style.SUCCESS(f'Copied reference data: {summary}.'))

    def _warn_on_release_mismatch(self) -> None:
        """Concepts resolve by code, so a different vocabulary release leaves gaps."""
        def version(using: str) -> str | None:
            return VocabularyRelease.objects.using(using).filter(status='published').order_by(
                '-published_at').values_list('athena_version', flat=True).first()

        source, here = version(SOURCE_ALIAS), version('default')
        if source != here:
            self.stdout.write(self.style.WARNING(
                f'  ! Vocabulary release differs: source {source}, here {here}. '
                'Concepts missing here are cleared and reported.'
            ))
