"""Apply production migrations without putting bulk data work on every web boot."""

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder

from omop_core.management.commands.load_athena_vocabularies import (
    required_hklabs_loinc_codes,
)
from omop_core.models import Concept


APP = 'omop_core'
BASELINE = '0200_merge_20260901_0000'
DEPENDENT_MIGRATION = '0201_seed_hklabs_sccm'


class Command(BaseCommand):
    help = 'Prepare the production database, bootstrapping migration 0201 when needed'

    def add_arguments(self, parser):
        # --lfs and --gdrive combine (LFS first, Drive fallback, #2288); the
        # local sources stand alone.
        parser.add_argument('--lfs', help='Git LFS repository publishing the Athena ZIP')
        parser.add_argument('--gdrive', help='Approved Athena vocabulary folder or ZIP URL')
        local = parser.add_mutually_exclusive_group()
        local.add_argument('--archive', help='Local approved Athena ZIP archive')
        local.add_argument('--path', help='Directory containing approved Athena TSV files')

    def handle(self, *args, **options):
        applied = (APP, DEPENDENT_MIGRATION) in MigrationRecorder(
            connection
        ).applied_migrations()
        if applied:
            self.stdout.write(
                f'{APP}.{DEPENDENT_MIGRATION} already applied; skipping Athena bootstrap.'
            )
            call_command('migrate', interactive=False, verbosity=options['verbosity'])
            return

        call_command(
            'migrate', APP, BASELINE, interactive=False, verbosity=options['verbosity'],
        )
        required = required_hklabs_loinc_codes()
        present = set(
            Concept.objects.filter(
                vocabulary_id='LOINC', concept_code__in=required,
            ).values_list('concept_code', flat=True)
        )
        missing = required - present
        if missing:
            source = {
                name: options.get(name) for name in ('lfs', 'gdrive', 'archive', 'path')
                if options.get(name)
            }
            if (source.get('lfs') or source.get('gdrive')) and (
                source.get('archive') or source.get('path')
            ):
                raise CommandError('--lfs/--gdrive cannot be combined with --archive or --path')
            if not source:
                raise CommandError(
                    'Migration 0201 requires missing LOINC concepts; provide '
                    '--lfs and/or --gdrive, or one of --archive, --path.'
                )
            self.stdout.write(
                f'Loading {len(missing)} migration-required LOINC concepts from Athena...'
            )
            call_command(
                'load_athena_vocabularies', **source, migration_bootstrap=True,
                verbosity=options['verbosity'],
            )

        remaining = set(
            Concept.objects.filter(
                vocabulary_id='LOINC', concept_code__in=required,
            ).values_list('concept_code', flat=True)
        )
        missing = sorted(required - remaining)
        if missing:
            self.stdout.write(self.style.WARNING(
                'Athena does not contain these historical LOINC concepts; '
                'migration 0201 will leave their mappings proposed: '
                + ', '.join(missing)
            ))
        call_command('migrate', interactive=False, verbosity=options['verbosity'])
