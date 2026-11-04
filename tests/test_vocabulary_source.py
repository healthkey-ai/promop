"""Where the Athena and LOINC archives come from: LFS first, Drive as fallback (#2288)."""
import hashlib
import json
from io import StringIO
from unittest.mock import Mock

import pytest
import requests
from django.core.management import CommandError, call_command
from django.utils import timezone

from omop_core.management.commands import queue_athena_vocabulary_sync as queue_cmd
from omop_core.management.commands import sync_athena_vocabulary as sync
from omop_core.models import AthenaVocabularySync
from omop_core.services import vocabulary_source as vs


LFS = 'https://github.com/healthkey-ai/omop-vocabularies'
GDRIVE = 'https://drive.google.com/drive/folders/test'
ATHENA_SHA = 'a' * 64
LOINC_SHA = 'b' * 64
MANIFEST = {
    'athena': {'file': 'vocabulary_download_v5_x.zip', 'sha256': ATHENA_SHA, 'size': 10},
    'loinc': {'file': 'Loinc_2.83.zip', 'sha256': LOINC_SHA, 'size': 5, 'version': '2.83'},
}


class _Response:
    def __init__(self, status=200, body=None, content=b''):
        self.status_code = status
        self._body = body
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'{self.status_code}')

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def iter_content(self, chunk_size):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _drive_selection(**overrides):
    return {
        'kind': 'gdrive', 'source_url': GDRIVE, 'id': 'drive-file', 'name': 'athena.zip',
        'identity': 'gdrive-file:drive-file:athena.zip',
        'loinc_archive_id': 'drive-loinc', 'loinc_archive_name': 'Loinc_2.83.zip',
        **overrides,
    }


# --- URLs -------------------------------------------------------------------

@pytest.mark.parametrize('url, expected', [
    (LFS, ('healthkey-ai', 'omop-vocabularies', 'main')),
    (LFS + '.git', ('healthkey-ai', 'omop-vocabularies', 'main')),
    (LFS + '/tree/athena-20260929', ('healthkey-ai', 'omop-vocabularies', 'athena-20260929')),
    (LFS + '/tree/release/2026', ('healthkey-ai', 'omop-vocabularies', 'release/2026')),
])
def test_github_repository_urls_resolve_to_owner_repo_and_ref(url, expected):
    assert vs.parse_lfs_url(url) == expected
    assert vs.is_lfs_url(url)


@pytest.mark.parametrize('url', [GDRIVE, 'https://github.com/only-owner', 'not a url'])
def test_other_urls_are_not_lfs_sources(url):
    assert not vs.is_lfs_url(url)


def test_manifest_and_media_urls_serve_content_not_pointers():
    assert vs.manifest_url(LFS + '/tree/v1') == (
        'https://raw.githubusercontent.com/healthkey-ai/omop-vocabularies/v1/manifest.json'
    )
    assert vs.media_url(LFS, 'Loinc_2.83.zip') == (
        'https://media.githubusercontent.com/media/healthkey-ai/omop-vocabularies/main/Loinc_2.83.zip'
    )
    # The Athena bundle's name carries braces; nothing in a name may become URL syntax.
    assert vs.media_url(LFS, 'vocabulary_download_v5_{uuid}_1.zip').endswith(
        '/main/vocabulary_download_v5_%7Buuid%7D_1.zip'
    )
    assert vs.media_url(LFS, 'a b#c.zip').endswith('/main/a%20b%23c.zip')


def test_sources_come_from_flags_then_environment(monkeypatch):
    monkeypatch.delenv(vs.LFS_ENV, raising=False)
    monkeypatch.delenv(vs.GDRIVE_ENV, raising=False)
    assert vs.configured_sources() == (None, None)
    monkeypatch.setenv(vs.LFS_ENV, LFS)
    monkeypatch.setenv(vs.GDRIVE_ENV, ' ')
    assert vs.configured_sources() == (LFS, None)
    assert vs.configured_sources(gdrive=GDRIVE) == (LFS, GDRIVE)


# --- resolving --------------------------------------------------------------

def test_lfs_manifest_is_resolved_first_without_touching_drive(monkeypatch):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(body=MANIFEST))
    drive = Mock(side_effect=AssertionError('Drive must not be consulted'))
    monkeypatch.setattr(vs, 'resolve_gdrive_vocabulary', drive)

    selection = vs.resolve_vocabulary_source(lfs=LFS, gdrive=GDRIVE)

    assert selection['kind'] == 'lfs'
    assert selection['sha256'] == ATHENA_SHA
    assert selection['identity'] == (
        f'lfs-file:{ATHENA_SHA}:vocabulary_download_v5_x.zip+loinc:{LOINC_SHA}:Loinc_2.83.zip'
    )
    assert selection['download_url'].endswith('/main/vocabulary_download_v5_x.zip')
    assert selection['loinc_archive_id'] == LOINC_SHA
    assert selection['fallback_gdrive_url'] == GDRIVE
    drive.assert_not_called()


def test_loinc_is_optional_in_the_manifest(monkeypatch):
    monkeypatch.setattr(
        vs.requests, 'get', lambda url, **kw: _Response(body={'athena': MANIFEST['athena']}),
    )
    selection = vs.resolve_lfs_vocabulary(LFS)
    assert selection['loinc_archive_id'] is None
    assert '+loinc' not in selection['identity']


@pytest.mark.parametrize('response', [
    _Response(status=404),
    _Response(body=ValueError('not json')),
    _Response(body=['not', 'an', 'object']),
    _Response(body={'loinc': MANIFEST['loinc']}),
    _Response(body={'athena': {'file': 'x.zip', 'sha256': 'short'}}),
    _Response(body={'athena': {'file': '../x.zip', 'sha256': ATHENA_SHA}}),
])
def test_an_unusable_manifest_falls_back_to_drive(monkeypatch, response):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: response)
    monkeypatch.setattr(vs, 'resolve_gdrive_vocabulary', lambda url, log: _drive_selection())
    log = []

    selection = vs.resolve_vocabulary_source(lfs=LFS, gdrive=GDRIVE, log=log.append)

    assert selection['kind'] == 'gdrive'
    assert selection['identity'] == 'gdrive-file:drive-file:athena.zip'
    assert 'fallback_from' in selection
    assert any('falling back to Google Drive' in line for line in log)


def test_a_network_failure_falls_back_to_drive(monkeypatch):
    monkeypatch.setattr(
        vs.requests, 'get', Mock(side_effect=requests.ConnectionError('no route')),
    )
    monkeypatch.setattr(vs, 'resolve_gdrive_vocabulary', lambda url, log: _drive_selection())
    assert vs.resolve_vocabulary_source(lfs=LFS, gdrive=GDRIVE)['kind'] == 'gdrive'


def test_lfs_failure_without_a_drive_url_is_an_error(monkeypatch):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(status=404))
    with pytest.raises(CommandError, match='LFS manifest unavailable'):
        vs.resolve_vocabulary_source(lfs=LFS, gdrive=None)


def test_no_source_at_all_is_an_error():
    with pytest.raises(CommandError, match='No vocabulary source'):
        vs.resolve_vocabulary_source(lfs=None, gdrive=None)


def test_drive_alone_never_asks_github(monkeypatch):
    monkeypatch.setattr(vs.requests, 'get', Mock(side_effect=AssertionError('no HTTP')))
    monkeypatch.setattr(vs, 'resolve_gdrive_vocabulary', lambda url, log: _drive_selection())
    selection = vs.resolve_vocabulary_source(lfs=None, gdrive=GDRIVE)
    assert selection['kind'] == 'gdrive'
    assert 'fallback_from' not in selection


# --- downloading ------------------------------------------------------------

def test_lfs_download_verifies_the_manifest_sha(monkeypatch, tmp_path):
    content = b'zip bytes' * 1000
    sha = hashlib.sha256(content).hexdigest()
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(content=content))

    dest = vs.download_lfs_file('https://x/y.zip', tmp_path / 'y.zip', sha)

    assert dest.read_bytes() == content
    assert not (tmp_path / 'y.zip.part').exists()
    with pytest.raises(vs.LfsSourceError, match='does not match the manifest'):
        vs.download_lfs_file('https://x/y.zip', tmp_path / 'z.zip', 'c' * 64)
    assert not (tmp_path / 'z.zip').exists()


def test_a_write_failure_is_an_lfs_error_so_the_fallback_still_applies(monkeypatch, tmp_path):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(content=b'data'))
    unwritable = tmp_path / 'missing' / 'y.zip'
    monkeypatch.setattr(vs.Path, 'mkdir', lambda self, **kw: None)
    with pytest.raises(vs.LfsSourceError, match='LFS download failed'):
        vs.download_lfs_file('https://x/y.zip', unwritable, None)


def test_failed_lfs_download_falls_back_to_drive_and_rewrites_the_selection(monkeypatch, tmp_path):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(status=403))
    monkeypatch.setattr(vs, 'resolve_gdrive_vocabulary', lambda url, log: _drive_selection())
    drive_archive = tmp_path / 'athena-vocabulary.zip'
    fake_loader = Mock()
    fake_loader._download_gdrive_vocabulary = Mock(return_value=drive_archive)
    monkeypatch.setattr(vs, '_gdrive', lambda: fake_loader)
    selection = {
        'kind': 'lfs', 'source_url': LFS, 'sha256': ATHENA_SHA, 'name': 'x.zip',
        'download_url': 'https://media/x.zip', 'identity': f'lfs-file:{ATHENA_SHA}:x.zip',
        'fallback_gdrive_url': GDRIVE,
    }
    log = []

    archive = vs.download_vocabulary_archive(selection, tmp_path, log.append)

    assert archive == drive_archive
    assert selection['kind'] == 'gdrive'
    assert selection['identity'] == 'gdrive-file:drive-file:athena.zip'
    fake_loader._download_gdrive_vocabulary.assert_called_once()
    assert fake_loader._download_gdrive_vocabulary.call_args.kwargs['selection'] is selection
    assert any('falling back to Google Drive' in line for line in log)


def test_failed_lfs_download_without_drive_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(status=403))
    selection = {
        'kind': 'lfs', 'source_url': LFS, 'sha256': ATHENA_SHA,
        'download_url': 'https://media/x.zip', 'fallback_gdrive_url': None,
    }
    with pytest.raises(CommandError, match='LFS download failed'):
        vs.download_vocabulary_archive(selection, tmp_path)


def test_failed_loinc_download_is_a_warning_not_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(vs.requests, 'get', lambda url, **kw: _Response(status=500))
    log = []
    selection = {
        'kind': 'lfs', 'loinc_archive_id': LOINC_SHA, 'loinc_archive_name': 'Loinc_2.83.zip',
        'loinc_download_url': 'https://media/Loinc_2.83.zip',
    }
    assert vs.download_loinc_archive(selection, tmp_path, log.append) is None
    assert any('LOINC axes will not be loaded' in line for line in log)
    assert vs.download_loinc_archive({'kind': 'lfs', 'loinc_archive_id': None}, tmp_path) is None


# --- manifest ---------------------------------------------------------------

def test_build_manifest_hashes_the_archives_and_reads_the_loinc_version(tmp_path):
    athena = tmp_path / 'vocabulary_download_v5_{uuid}_123.zip'
    athena.write_bytes(b'athena')
    (tmp_path / 'Loinc_2.83.zip').write_bytes(b'loinc')

    manifest = vs.build_manifest(tmp_path, released='2026-11-04')

    assert manifest['athena'] == {
        'file': athena.name, 'sha256': hashlib.sha256(b'athena').hexdigest(), 'size': 6,
    }
    assert manifest['loinc']['version'] == '2.83'
    assert json.loads((tmp_path / 'manifest.json').read_text()) == manifest
    # The manifest it wrote is one the resolver accepts.
    assert vs._manifest_entry(manifest, 'athena')['name'] == athena.name


# --- the sync command -------------------------------------------------------

def _sync_options(**overrides):
    return {
        'lfs': None, 'gdrive': None, 'source': None, 'archive': None, 'path': None,
        'apply': False, 'dry_run': False, **overrides,
    }


def test_sync_recognises_an_installed_sha_from_the_manifest_without_downloading(monkeypatch):
    """Switching a deployment from Drive to LFS must not reload an unchanged vocabulary."""
    command = sync.Command(stdout=StringIO())
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    monkeypatch.setattr(
        vs, 'resolve_vocabulary_source',
        lambda **kw: {'kind': 'lfs', 'sha256': ATHENA_SHA, 'name': 'x.zip',
                      'identity': f'lfs-file:{ATHENA_SHA}:x.zip'},
    )
    # The identity was never recorded (previous syncs ran from Drive)...
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: False)
    # ...but the archive's SHA-256 was.
    monkeypatch.setattr(command, '_sha_is_current', lambda digest: digest == ATHENA_SHA)
    receipt = Mock()
    monkeypatch.setattr(command, '_receipt', receipt)
    download = Mock(side_effect=AssertionError('current artifacts must not download'))
    monkeypatch.setattr(vs, 'download_vocabulary_archive', download)

    command.handle(**_sync_options(lfs=LFS, gdrive=GDRIVE))

    download.assert_not_called()
    assert receipt.call_args.args[6] == 'current'


@pytest.mark.django_db
def test_sync_loads_only_the_loinc_axes_when_the_athena_sha_is_current(monkeypatch, tmp_path):
    """The identity names the LOINC release too; a new one with an unchanged bundle still loads."""
    AthenaVocabularySync.objects.create(
        source_url=GDRIVE, outcome='applied', started_at=timezone.now(),
        source_artifact_identity=f'gdrive-file:f:x.zip+loinc:old:Loinc_2.82.zip',
        source_artifact_sha256=ATHENA_SHA,
    )
    command = sync.Command(stdout=StringIO())
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    selection = {
        'kind': 'lfs', 'sha256': ATHENA_SHA, 'name': 'x.zip',
        'identity': f'lfs-file:{ATHENA_SHA}:x.zip+loinc:{LOINC_SHA}:Loinc_2.83.zip',
        'loinc_archive_id': LOINC_SHA, 'loinc_archive_name': 'Loinc_2.83.zip',
    }
    monkeypatch.setattr(vs, 'resolve_vocabulary_source', lambda **kw: selection)
    download = Mock(side_effect=AssertionError('the Athena archive must not download'))
    monkeypatch.setattr(vs, 'download_vocabulary_archive', download)
    loinc_zip = tmp_path / 'Loinc_2.83.zip'
    monkeypatch.setattr(vs, 'download_loinc_archive', lambda *a, **k: loinc_zip)
    loaded = Mock(return_value=100)
    monkeypatch.setattr(sync, 'load_loinc_axes_from_archive', loaded)

    command.handle(**_sync_options(lfs=LFS, gdrive=GDRIVE))

    download.assert_not_called()
    loaded.assert_called_once()
    assert loaded.call_args.args[0] == loinc_zip
    receipt = AthenaVocabularySync.objects.latest('pk')
    assert receipt.outcome == 'current'
    assert receipt.source_artifact_sha256 == ATHENA_SHA
    assert receipt.source_artifact_identity == selection['identity']

    # Recorded now, so the next run is a metadata-only no-op.
    loaded.reset_mock()
    command.handle(**_sync_options(lfs=LFS, gdrive=GDRIVE))
    loaded.assert_not_called()


@pytest.mark.django_db
def test_sync_does_not_reload_a_loinc_release_it_already_recorded(monkeypatch):
    identity = f'gdrive-file:f:x.zip+loinc:{LOINC_SHA}:Loinc_2.83.zip'
    AthenaVocabularySync.objects.create(
        source_url=GDRIVE, outcome='current', started_at=timezone.now(),
        source_artifact_identity=identity, source_artifact_sha256=ATHENA_SHA,
    )
    assert sync.Command._loinc_is_current(f'lfs-file:{ATHENA_SHA}:y.zip+loinc:{LOINC_SHA}:Loinc_2.83.zip')
    assert not sync.Command._loinc_is_current(f'lfs-file:{ATHENA_SHA}:y.zip+loinc:{"c" * 64}:Loinc_2.84.zip')
    assert sync.Command._loinc_is_current('lfs-file:no-loinc-at-all')


def test_sync_identity_follows_a_download_that_fell_back_to_drive(tmp_path, monkeypatch):
    archive = tmp_path / 'athena.zip'
    archive.write_bytes(b'changed')
    command = sync.Command(stdout=StringIO())
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    monkeypatch.setattr(
        vs, 'resolve_vocabulary_source',
        lambda **kw: {'kind': 'lfs', 'sha256': ATHENA_SHA, 'name': 'x.zip',
                      'identity': f'lfs-file:{ATHENA_SHA}:x.zip', 'fallback_gdrive_url': GDRIVE},
    )

    def download(selection, directory, log):
        selection.clear()
        selection.update(_drive_selection())
        return archive

    monkeypatch.setattr(vs, 'download_vocabulary_archive', download)
    monkeypatch.setattr(vs, 'download_loinc_archive', lambda *a, **k: None)
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: False)
    monkeypatch.setattr(command, '_sha_is_current', lambda digest: False)
    receipt = Mock()
    monkeypatch.setattr(command, '_receipt', receipt)

    with pytest.raises(CommandError, match='--dry-run.*--apply'):
        command.handle(**_sync_options(lfs=LFS, gdrive=GDRIVE))

    assert receipt.call_args.args[0] == GDRIVE
    assert receipt.call_args.args[1] == 'gdrive-file:drive-file:athena.zip'


def test_sync_without_a_source_tries_lfs_then_the_drive_default(monkeypatch):
    command = sync.Command(stdout=StringIO())
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    seen = {}

    def resolve(**kw):
        seen.update(kw)
        return {'kind': 'lfs', 'sha256': ATHENA_SHA, 'identity': 'lfs-file:x'}

    monkeypatch.setattr(vs, 'resolve_vocabulary_source', resolve)
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: True)
    monkeypatch.setattr(command, '_receipt', Mock())

    command.handle(**_sync_options())

    assert seen['lfs'] == vs.DEFAULT_LFS_URL
    assert seen['gdrive'] == sync.DEFAULT_GDRIVE_URL


def test_a_queued_lfs_source_falls_back_to_the_environment_drive_url(monkeypatch):
    monkeypatch.setenv(vs.GDRIVE_ENV, GDRIVE)
    assert sync.Command._remote_sources(_sync_options(source=LFS)) == (LFS, GDRIVE)
    monkeypatch.delenv(vs.GDRIVE_ENV)
    assert sync.Command._remote_sources(_sync_options(source=LFS)) == (LFS, sync.DEFAULT_GDRIVE_URL)
    # A receipt queued before #2288 names the Drive folder and stays Drive-only.
    assert sync.Command._remote_sources(_sync_options(source=GDRIVE)) == (None, GDRIVE)
    with pytest.raises(CommandError, match='--source cannot be combined'):
        sync.Command._remote_sources(_sync_options(source=LFS, gdrive=GDRIVE))


def test_sync_refuses_remote_and_local_sources_together():
    command = sync.Command(stdout=StringIO())
    with pytest.raises(CommandError, match='cannot be combined with --archive or --path'):
        command.handle(**_sync_options(lfs=LFS, archive='/tmp/x.zip'))


# --- queueing ---------------------------------------------------------------

@pytest.mark.django_db
def test_queue_records_the_lfs_url_as_the_primary_source(monkeypatch):
    monkeypatch.setattr(
        'omop_core.tasks.sync_athena_vocabulary_task.apply_async',
        lambda *a, **k: Mock(id='task-1'),
    )
    command = queue_cmd.Command(stdout=StringIO())

    command.handle(lfs=LFS, gdrive=GDRIVE)

    assert AthenaVocabularySync.objects.get().source_url == LFS


@pytest.mark.django_db
def test_queue_with_only_drive_still_queues_drive(monkeypatch):
    monkeypatch.setattr(
        'omop_core.tasks.sync_athena_vocabulary_task.apply_async',
        lambda *a, **k: Mock(id='task-1'),
    )
    queue_cmd.Command(stdout=StringIO()).handle(lfs=None, gdrive=GDRIVE)
    assert AthenaVocabularySync.objects.get().source_url == GDRIVE


def test_queue_needs_a_source():
    with pytest.raises(CommandError, match='--lfs and/or --gdrive'):
        queue_cmd.Command(stdout=StringIO()).handle(lfs=None, gdrive=None)


@pytest.mark.django_db
def test_worker_passes_the_receipt_source_to_the_sync_command(monkeypatch):
    from django.core import management
    from django.utils import timezone
    from omop_core.tasks import sync_athena_vocabulary_task

    row = AthenaVocabularySync.objects.create(
        source_url=LFS, outcome='queued', started_at=timezone.now(),
    )
    seen = {}

    def command(name, **options):
        seen.update(options)
        AthenaVocabularySync.objects.filter(pk=row.pk).update(outcome='current')

    monkeypatch.setattr(management, 'call_command', command)
    sync_athena_vocabulary_task.run(row.pk)

    assert seen['source'] == LFS
    assert 'gdrive' not in seen


# --- the loader and the bootstrap ------------------------------------------

def _archive_with_concept(path):
    import zipfile
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr('CONCEPT.csv', 'concept_id\n')
    return path


@pytest.mark.django_db
def test_loader_resolves_lfs_then_drive_and_takes_the_published_loinc(monkeypatch, tmp_path):
    from omop_core.management.commands import load_athena_vocabularies as loader

    archive = _archive_with_concept(tmp_path / 'athena.zip')
    loinc_zip = tmp_path / 'Loinc_2.83.zip'
    seen = {}
    monkeypatch.setattr(vs, 'resolve_vocabulary_source', lambda **kw: (
        seen.update(kw) or {'kind': 'lfs', 'loinc_archive_id': LOINC_SHA}
    ))
    monkeypatch.setattr(vs, 'download_vocabulary_archive', lambda *a, **k: archive)
    monkeypatch.setattr(vs, 'download_loinc_archive', lambda *a, **k: loinc_zip)
    monkeypatch.setattr(
        loader.Command, '_load_relationships', Mock(side_effect=CommandError('stop here')),
    )
    command = loader.Command(stdout=StringIO())
    with pytest.raises(CommandError, match='stop here'):
        call_command(command, lfs=LFS, gdrive=GDRIVE, skip_umls_cache=True)

    assert seen == {'lfs': LFS, 'gdrive': GDRIVE, 'log': command._log}
    assert command._loinc_archive_path == loinc_zip


@pytest.mark.django_db
@pytest.mark.parametrize('mode', ['dry_run', 'migration_bootstrap'])
def test_loader_skips_the_loinc_download_when_it_would_not_load_it(monkeypatch, tmp_path, mode):
    from omop_core.management.commands import load_athena_vocabularies as loader

    archive = _archive_with_concept(tmp_path / 'athena.zip')
    monkeypatch.setattr(
        vs, 'resolve_vocabulary_source', lambda **kw: {'kind': 'lfs', 'loinc_archive_id': LOINC_SHA},
    )
    monkeypatch.setattr(vs, 'download_vocabulary_archive', lambda *a, **k: archive)
    loinc = Mock(side_effect=AssertionError('93 MB the web boot would discard'))
    monkeypatch.setattr(vs, 'download_loinc_archive', loinc)
    monkeypatch.setattr(
        loader.Command, '_load_relationships', Mock(side_effect=CommandError('stop here')),
    )
    with pytest.raises(CommandError, match='stop here'):
        call_command(
            'load_athena_vocabularies', lfs=LFS, skip_umls_cache=True,
            stdout=StringIO(), **{mode: True},
        )
    loinc.assert_not_called()


@pytest.mark.django_db
def test_loader_keeps_an_explicit_loinc_archive_over_the_published_one(monkeypatch, tmp_path):
    from omop_core.management.commands import load_athena_vocabularies as loader

    archive = _archive_with_concept(tmp_path / 'athena.zip')
    monkeypatch.setattr(
        vs, 'resolve_vocabulary_source', lambda **kw: {'kind': 'lfs', 'loinc_archive_id': LOINC_SHA},
    )
    monkeypatch.setattr(vs, 'download_vocabulary_archive', lambda *a, **k: archive)
    loinc = Mock(side_effect=AssertionError('--loinc-archive wins'))
    monkeypatch.setattr(vs, 'download_loinc_archive', loinc)
    monkeypatch.setattr(
        loader.Command, '_load_relationships', Mock(side_effect=CommandError('stop here')),
    )
    with pytest.raises(CommandError, match='stop here'):
        call_command(
            'load_athena_vocabularies', lfs=LFS, skip_umls_cache=True,
            loinc_archive=str(tmp_path / 'mine.zip'), stdout=StringIO(),
        )
    loinc.assert_not_called()


@pytest.mark.django_db
def test_prepare_passes_both_sources_to_the_bootstrap_loader(monkeypatch):
    from django.db.migrations.recorder import MigrationRecorder
    from omop_core.management.commands import prepare_production_database as preparation

    monkeypatch.setattr(MigrationRecorder, 'applied_migrations', lambda self: {})
    monkeypatch.setattr(preparation, 'required_hklabs_loinc_codes', lambda: frozenset({'X'}))
    calls = []
    monkeypatch.setattr(preparation, 'call_command', lambda name, *a, **kw: calls.append((name, kw)))

    call_command('prepare_production_database', lfs=LFS, gdrive=GDRIVE)

    name, kwargs = calls[1]
    assert name == 'load_athena_vocabularies'
    assert kwargs['lfs'] == LFS and kwargs['gdrive'] == GDRIVE
    assert kwargs['migration_bootstrap'] is True


@pytest.mark.django_db
def test_prepare_rejects_remote_and_local_sources_together_before_migrating(monkeypatch):
    from omop_core.management.commands import prepare_production_database as preparation

    migrate = Mock()
    monkeypatch.setattr(preparation, 'call_command', migrate)
    with pytest.raises(CommandError, match='cannot be combined'):
        call_command('prepare_production_database', lfs=LFS, archive='/tmp/x.zip')
    migrate.assert_not_called()
