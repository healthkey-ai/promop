"""Keep an installed Athena vocabulary aligned with the governed Drive artifact."""

import hashlib
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from omop_core.management.commands.load_athena_vocabularies import (
    DEFAULT_GDRIVE_URL,
    _download_gdrive_vocabulary,
    _resolve_gdrive_vocabulary,
    _sha256_file,
)
from omop_core.models import AthenaVocabularySync, VocabularyRelease


LOCK_ID = 1_652_202_609
TABLE_MODELS = {
    'relationship': 'Relationship',
    'vocabulary': 'Vocabulary',
    'domain': 'Domain',
    'concept_class': 'ConceptClass',
    'concept': 'Concept',
    'concept_relationship': 'ConceptRelationship',
    'concept_ancestor': 'ConceptAncestor',
    'concept_synonym': 'ConceptSynonym',
    'drug_strength': 'DrugStrength',
    'source_to_concept_map': 'SourceToConceptMap',
}


def _path_sha256(path):
    """Fingerprint the supported source files without depending on mtimes."""
    digest = hashlib.sha256()
    root = Path(path)
    for filename in sorted(f'{name.upper()}.csv' for name in TABLE_MODELS):
        source = root / filename
        if not source.exists():
            continue
        digest.update(filename.encode())
        with source.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    return digest.hexdigest()


def _table_counts():
    from django.apps import apps

    return {
        table: apps.get_model('omop_core', model).objects.count()
        for table, model in TABLE_MODELS.items()
    }


class Command(BaseCommand):
    help = (
        'Check the installed Athena artifact and optionally apply an insert-only '
        'delta from the governed source.'
    )

    def add_arguments(self, parser):
        sources = parser.add_mutually_exclusive_group()
        sources.add_argument('--gdrive', nargs='?', const=DEFAULT_GDRIVE_URL)
        sources.add_argument('--archive')
        sources.add_argument('--path')
        action = parser.add_mutually_exclusive_group()
        action.add_argument('--apply', action='store_true')
        action.add_argument('--dry-run', action='store_true', dest='dry_run')

    def handle(self, *args, **options):
        started_at = timezone.now()
        gdrive = options.get('gdrive')
        archive = options.get('archive')
        source_path = options.get('path')
        if not any((gdrive, archive, source_path)):
            gdrive = DEFAULT_GDRIVE_URL
        source_url = gdrive or archive or source_path
        identity = ''
        sha256 = ''
        previous = self._latest_release()

        try:
            with tempfile.TemporaryDirectory(prefix='promop-athena-sync-') as directory:
                selection = None
                if gdrive:
                    selection = _resolve_gdrive_vocabulary(gdrive, self.stdout.write)
                    identity = selection['identity']
                    if self._identity_is_current(identity):
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'Athena vocabulary is current ({identity}); no archive download or table work.'
                            )
                        )
                        return
                    archive = _download_gdrive_vocabulary(
                        gdrive, Path(directory), self.stdout.write, selection=selection,
                    )

                if archive:
                    archive = str(archive)
                    sha256 = _sha256_file(archive)
                else:
                    sha256 = _path_sha256(source_path)
                identity = identity or f'sha256:{sha256}'

                if self._sha_is_current(sha256):
                    self._receipt(
                        source_url, identity, sha256, previous, previous, {}, 'current', started_at,
                    )
                    self.stdout.write(self.style.SUCCESS(
                        f'Athena vocabulary content is current (sha256:{sha256}); no table work.'
                    ))
                    return

                if not options['apply'] and not options['dry_run']:
                    self._receipt(
                        source_url, identity, sha256, previous, None, {},
                        'delta_available', started_at,
                    )
                    raise CommandError(
                        'The configured Athena artifact differs from the installed release. '
                        'Run again with --dry-run to measure the missing-row delta or --apply to install it.'
                    )

                missing, installed, outcome = self._load_delta(
                    archive=archive, source_path=source_path, identity=identity,
                    sha256=sha256, dry_run=options['dry_run'],
                )
                self._receipt(
                    source_url, identity, sha256, previous, installed, missing,
                    outcome, started_at,
                )
                summary = ', '.join(f'{table}={count}' for table, count in missing.items())
                if outcome == 'applied':
                    self.stdout.write(self.style.SUCCESS(f'Applied Athena missing-row delta: {summary}'))
                elif outcome == 'dry_run':
                    self.stdout.write(f'Athena missing-row delta (dry run, rolled back): {summary}')
                else:
                    self.stdout.write(self.style.SUCCESS(
                        'Athena artifact contains no rows missing from this database; no release published.'
                    ))
        except Exception as exc:
            if not isinstance(exc, CommandError) or 'differs from the installed release' not in str(exc):
                self._receipt(
                    source_url, identity, sha256, previous, None, {}, 'failed',
                    started_at, str(exc),
                )
            raise

    def _load_delta(self, archive, source_path, identity, sha256, dry_run):
        installed = None
        with transaction.atomic():
            self._acquire_lock()
            if self._sha_is_current(sha256):
                return {}, self._latest_release(), 'current'
            prior_release = self._latest_release()
            before = _table_counts()
            source = {'archive': archive} if archive else {'path': source_path}
            call_command(
                'load_athena_vocabularies', **source,
                skip_umls_cache=True, skip_code_mappings=True,
                verbosity=0, stdout=self.stdout, stderr=self.stderr,
            )
            after = _table_counts()
            missing = {
                table: after[table] - count for table, count in before.items()
                if after[table] != count
            }
            installed = self._latest_release()
            if dry_run or not missing:
                transaction.set_rollback(True)
                return missing, None if dry_run else prior_release, (
                    'dry_run' if dry_run else 'current'
                )
            installed.source_artifact_identity = identity
            installed.source_artifact_sha256 = sha256
            installed.save(update_fields=[
                'source_artifact_identity', 'source_artifact_sha256',
            ])
        return missing, installed, 'applied'

    @staticmethod
    def _acquire_lock():
        if connection.vendor != 'postgresql':
            return
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_xact_lock(%s)', [LOCK_ID])
            if not cursor.fetchone()[0]:
                raise CommandError('Another Athena freshness check or delta load is already running.')

    @staticmethod
    def _latest_release():
        return (
            VocabularyRelease.objects.filter(status='published')
            .order_by('-published_at', '-pk').first()
        )

    @staticmethod
    def _identity_is_current(identity):
        return AthenaVocabularySync.objects.filter(
            source_artifact_identity=identity,
            outcome__in=('current', 'applied'),
        ).exists() or VocabularyRelease.objects.filter(
            status='published', source_artifact_identity=identity,
        ).exists()

    @staticmethod
    def _sha_is_current(sha256):
        return AthenaVocabularySync.objects.filter(
            source_artifact_sha256=sha256,
            outcome__in=('current', 'applied'),
        ).exists() or VocabularyRelease.objects.filter(
            status='published', source_artifact_sha256=sha256,
        ).exists()

    @staticmethod
    def _receipt(source_url, identity, sha256, previous, installed, missing,
                 outcome, started_at, failure_reason=''):
        return AthenaVocabularySync.objects.create(
            source_url=str(source_url),
            source_artifact_identity=identity,
            source_artifact_sha256=sha256,
            previous_release=previous,
            installed_release=installed,
            missing_rows=missing,
            outcome=outcome,
            failure_reason=failure_reason,
            started_at=started_at,
        )
