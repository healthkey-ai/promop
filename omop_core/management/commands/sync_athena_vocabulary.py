"""Keep an installed Athena vocabulary aligned with the governed artifact.

The artifact comes from the Git LFS repository first and the Drive folder as
fallback (#2288); see ``omop_core.services.vocabulary_source``.
"""

import hashlib
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from omop_core.management.commands.load_athena_vocabularies import (
    DEFAULT_GDRIVE_URL,
    _sha256_file,
)
from omop_core.models import AthenaVocabularySync, VocabularyRelease
from omop_core.services import vocabulary_source
from omop_core.services.vocabulary_source import DEFAULT_LFS_URL


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
        parser.add_argument('--lfs', nargs='?', const=DEFAULT_LFS_URL,
                            help='Git LFS repository URL; tried before --gdrive.')
        parser.add_argument('--gdrive', nargs='?', const=DEFAULT_GDRIVE_URL,
                            help='Google Drive folder or file URL; the fallback when --lfs is given.')
        parser.add_argument('--source', help=(
            'A queued receipt\'s source URL (worker use): classified as LFS or '
            'Drive by its host. An LFS source falls back to ATHENA_VOCABULARY_GDRIVE_URL.'
        ))
        local = parser.add_mutually_exclusive_group()
        local.add_argument('--archive')
        local.add_argument('--path')
        action = parser.add_mutually_exclusive_group()
        action.add_argument('--apply', action='store_true')
        action.add_argument('--dry-run', action='store_true', dest='dry_run')
        parser.add_argument(
            '--sync-id', type=int,
            help='Update an existing queued sync receipt (worker use only).',
        )

    def handle(self, *args, **options):
        self._sync_id = options.get('sync_id')
        started_at = timezone.now()
        lfs, gdrive = self._remote_sources(options)
        archive = options.get('archive')
        source_path = options.get('path')
        if (lfs or gdrive) and (archive or source_path):
            raise CommandError('--lfs/--gdrive cannot be combined with --archive or --path')
        if not any((lfs, gdrive, archive, source_path)):
            lfs, gdrive = DEFAULT_LFS_URL, DEFAULT_GDRIVE_URL
        source_url = lfs or gdrive or archive or source_path
        remote = bool(lfs or gdrive)
        identity = ''
        sha256 = ''
        previous = self._latest_release()

        try:
            with tempfile.TemporaryDirectory(prefix='promop-athena-sync-') as directory:
                selection = None
                loinc_archive = None
                if remote:
                    selection = vocabulary_source.resolve_vocabulary_source(
                        lfs=lfs, gdrive=gdrive, log=self.stdout.write,
                    )
                    identity = selection['identity']
                    # An LFS manifest names the archive's SHA-256, so a release
                    # already installed from Drive is recognised before any
                    # download: switching sources never costs a reload.
                    manifest_sha = selection.get('sha256') or ''
                    if self._identity_is_current(identity) or (
                        manifest_sha and self._sha_is_current(manifest_sha)
                    ):
                        self._receipt(
                            source_url, identity, '', previous, previous, {},
                            'current', started_at,
                        )
                        self.stdout.write(
                            self.style.SUCCESS(
                                f'Athena vocabulary is current ({identity}); no archive download or table work.'
                            )
                        )
                        return
                    archive = vocabulary_source.download_vocabulary_archive(
                        selection, Path(directory), self.stdout.write,
                    )
                    # The download may have fallen back to Drive, which
                    # rewrites the selection; the identity must follow it.
                    identity = selection['identity']
                    loinc_archive = vocabulary_source.download_loinc_archive(
                        selection, Path(directory), self.stdout.write,
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
                    loinc_archive=str(loinc_archive) if loinc_archive else None,
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

    @staticmethod
    def _remote_sources(options):
        """``(lfs, gdrive)`` from the explicit flags or a receipt's ``--source``."""
        lfs = options.get('lfs')
        gdrive = options.get('gdrive')
        source = options.get('source')
        if source:
            if lfs or gdrive:
                raise CommandError('--source cannot be combined with --lfs or --gdrive')
            if vocabulary_source.is_lfs_url(source):
                lfs = source
                _, gdrive = vocabulary_source.configured_sources()
                gdrive = gdrive or DEFAULT_GDRIVE_URL
            else:
                gdrive = source
        return lfs, gdrive

    def _load_delta(self, archive, source_path, identity, sha256, dry_run,
                    loinc_archive=None):
        installed = None
        with transaction.atomic():
            self._acquire_lock()
            if self._sha_is_current(sha256):
                return {}, self._latest_release(), 'current'
            prior_release = self._latest_release()
            before = _table_counts()
            source = {'archive': archive} if archive else {'path': source_path}
            extra = {}
            if loinc_archive:
                extra['loinc_archive'] = loinc_archive
            call_command(
                'load_athena_vocabularies', **source, **extra,
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

    def _receipt(self, source_url, identity, sha256, previous, installed, missing,
                 outcome, started_at, failure_reason=''):
        values = dict(
            source_url=str(source_url),
            source_artifact_identity=identity,
            source_artifact_sha256=sha256,
            previous_release=previous,
            installed_release=installed,
            missing_rows=missing,
            outcome=outcome,
            failure_reason=failure_reason,
            started_at=started_at,
            completed_at=timezone.now(),
        )
        if self._sync_id:
            updated = AthenaVocabularySync.objects.filter(pk=self._sync_id).update(**values)
            if not updated:
                raise CommandError(f'Athena sync receipt {self._sync_id} does not exist.')
            return AthenaVocabularySync.objects.get(pk=self._sync_id)
        return AthenaVocabularySync.objects.create(**values)
