from io import StringIO
from unittest.mock import Mock

import pytest
from django.core.management import CommandError
from django.utils import timezone

from omop_core.management.commands import sync_athena_vocabulary as sync
from omop_core.models import AthenaVocabularySync, Relationship, VocabularyRelease
from omop_core.services import athena_vocabulary_jobs


def _options(**overrides):
    return {
        'gdrive': 'https://drive.google.com/drive/folders/test',
        'archive': None,
        'path': None,
        'apply': False,
        'dry_run': False,
        **overrides,
    }


def test_current_drive_identity_skips_archive_download_and_table_work(monkeypatch):
    command = sync.Command(stdout=StringIO())
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    monkeypatch.setattr(
        sync, '_resolve_gdrive_vocabulary',
        lambda *_args: {'id': 'current-file', 'name': 'athena.zip',
                        'identity': 'gdrive-file:current-file'},
    )
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: True)
    receipt = Mock()
    monkeypatch.setattr(command, '_receipt', receipt)
    download = Mock(side_effect=AssertionError('current artifacts must not download'))
    monkeypatch.setattr(sync, '_download_gdrive_vocabulary', download)

    command.handle(**_options())

    download.assert_not_called()
    assert receipt.call_args.args[6] == 'current'
    assert 'no archive download or table work' in command.stdout.getvalue()


def test_changed_artifact_requires_explicit_dry_run_or_apply(tmp_path, monkeypatch):
    archive = tmp_path / 'athena.zip'
    archive.write_bytes(b'changed')
    command = sync.Command(stdout=StringIO())
    receipt = Mock()
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    monkeypatch.setattr(
        sync, '_resolve_gdrive_vocabulary',
        lambda *_args: {'id': 'changed-file', 'name': 'athena.zip',
                        'identity': 'gdrive-file:changed-file'},
    )
    monkeypatch.setattr(sync, '_download_gdrive_vocabulary', lambda *_args, **_kwargs: archive)
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: False)
    monkeypatch.setattr(command, '_sha_is_current', lambda digest: False)
    monkeypatch.setattr(command, '_receipt', receipt)

    with pytest.raises(CommandError, match='--dry-run.*--apply'):
        command.handle(**_options())

    assert receipt.call_args.args[6] == 'delta_available'


def test_apply_records_the_measured_missing_row_delta(tmp_path, monkeypatch):
    archive = tmp_path / 'athena.zip'
    archive.write_bytes(b'changed')
    command = sync.Command(stdout=StringIO())
    receipt = Mock()
    installed = object()
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    monkeypatch.setattr(
        sync, '_resolve_gdrive_vocabulary',
        lambda *_args: {'id': 'changed-file', 'name': 'athena.zip',
                        'identity': 'gdrive-file:changed-file'},
    )
    monkeypatch.setattr(sync, '_download_gdrive_vocabulary', lambda *_args, **_kwargs: archive)
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: False)
    monkeypatch.setattr(command, '_sha_is_current', lambda digest: False)
    monkeypatch.setattr(
        command, '_load_delta',
        lambda **_kwargs: ({'concept': 2, 'relationship': 1}, installed, 'applied'),
    )
    monkeypatch.setattr(command, '_receipt', receipt)

    command.handle(**_options(apply=True))

    assert receipt.call_args.args[5] == {'concept': 2, 'relationship': 1}
    assert receipt.call_args.args[6] == 'applied'
    assert 'concept=2' in command.stdout.getvalue()


def test_failed_delta_gets_a_durable_failure_receipt(tmp_path, monkeypatch):
    archive = tmp_path / 'athena.zip'
    archive.write_bytes(b'changed')
    command = sync.Command(stdout=StringIO())
    receipt = Mock()
    monkeypatch.setattr(command, '_latest_release', lambda: None)
    monkeypatch.setattr(
        sync, '_resolve_gdrive_vocabulary',
        lambda *_args: {'id': 'changed-file', 'name': 'athena.zip',
                        'identity': 'gdrive-file:changed-file'},
    )
    monkeypatch.setattr(sync, '_download_gdrive_vocabulary', lambda *_args, **_kwargs: archive)
    monkeypatch.setattr(command, '_identity_is_current', lambda identity: False)
    monkeypatch.setattr(command, '_sha_is_current', lambda digest: False)
    monkeypatch.setattr(
        command, '_load_delta', Mock(side_effect=CommandError('invalid archive')),
    )
    monkeypatch.setattr(command, '_receipt', receipt)

    with pytest.raises(CommandError, match='invalid archive'):
        command.handle(**_options(apply=True))

    assert receipt.call_args.args[6] == 'failed'
    assert receipt.call_args.args[8] == 'invalid archive'


def test_path_fingerprint_ignores_mtime_and_unrelated_files(tmp_path):
    (tmp_path / 'CONCEPT.csv').write_text('concept_id\n1\n')
    before = sync._path_sha256(tmp_path)
    (tmp_path / 'README.txt').write_text('operator note')
    assert sync._path_sha256(tmp_path) == before
    (tmp_path / 'CONCEPT.csv').write_text('concept_id\n1\n2\n')
    assert sync._path_sha256(tmp_path) != before


def _release(version):
    now = timezone.now()
    return VocabularyRelease.objects.create(
        build_timestamp=now, athena_version=version, status='published',
        published_at=now,
    )


@pytest.mark.django_db
def test_apply_commits_missing_rows_and_tags_the_new_release(monkeypatch):
    _release('old')

    def load(*_args, **_kwargs):
        Relationship.objects.create(
            relationship_id='Delta relationship', relationship_name='Delta',
            is_hierarchical=0, defines_ancestry=0,
            reverse_relationship_id='Delta reverse', relationship_concept_id=0,
        )
        _release('new')

    monkeypatch.setattr(sync, 'call_command', load)
    command = sync.Command(stdout=StringIO())
    missing, installed, outcome = command._load_delta(
        archive='/tmp/not-read.zip', source_path=None,
        identity='gdrive-file:new', sha256='a' * 64, dry_run=False,
    )

    assert outcome == 'applied'
    assert missing == {'relationship': 1}
    assert Relationship.objects.filter(pk='Delta relationship').exists()
    installed.refresh_from_db()
    assert installed.source_artifact_identity == 'gdrive-file:new'
    assert installed.source_artifact_sha256 == 'a' * 64


@pytest.mark.django_db
def test_dry_run_rolls_back_rows_and_release(monkeypatch):
    previous = _release('old')

    def load(*_args, **_kwargs):
        Relationship.objects.create(
            relationship_id='Dry-run relationship', relationship_name='Dry run',
            is_hierarchical=0, defines_ancestry=0,
            reverse_relationship_id='Dry reverse', relationship_concept_id=0,
        )
        _release('new')

    monkeypatch.setattr(sync, 'call_command', load)
    command = sync.Command(stdout=StringIO())
    missing, installed, outcome = command._load_delta(
        archive='/tmp/not-read.zip', source_path=None,
        identity='gdrive-file:new', sha256='b' * 64, dry_run=True,
    )

    assert outcome == 'dry_run'
    assert missing == {'relationship': 1}
    assert installed is None
    assert not Relationship.objects.filter(pk='Dry-run relationship').exists()
    assert list(VocabularyRelease.objects.values_list('pk', flat=True)) == [previous.pk]


@pytest.mark.django_db
def test_enqueue_is_durable_and_deduplicates_active_work(monkeypatch):
    queued = Mock(id='celery-task-1')
    monkeypatch.setattr(
        'omop_core.tasks.sync_athena_vocabulary_task.apply_async',
        Mock(return_value=queued),
    )

    first, created = athena_vocabulary_jobs.enqueue_sync('https://example.test/athena')
    second, duplicate = athena_vocabulary_jobs.enqueue_sync('https://example.test/athena')

    assert created is True
    assert duplicate is False
    assert second.pk == first.pk
    first.refresh_from_db()
    assert first.outcome == 'queued'
    assert first.task_id == 'celery-task-1'


@pytest.mark.django_db
def test_enqueue_failure_is_recorded_and_can_be_retried(monkeypatch):
    publish = Mock(side_effect=RuntimeError('broker unavailable'))
    monkeypatch.setattr(
        'omop_core.tasks.sync_athena_vocabulary_task.apply_async', publish,
    )

    with pytest.raises(RuntimeError, match='broker unavailable'):
        athena_vocabulary_jobs.enqueue_sync('https://example.test/athena')

    failed = AthenaVocabularySync.objects.get()
    assert failed.outcome == 'failed'
    assert failed.completed_at is not None

    publish.side_effect = None
    publish.return_value = Mock(id='retry-task')
    retried, created = athena_vocabulary_jobs.enqueue_sync('https://example.test/athena')
    assert created is True
    assert retried.pk != failed.pk


@pytest.mark.django_db
def test_worker_updates_the_queued_receipt(monkeypatch):
    from django.core import management
    from omop_core.tasks import sync_athena_vocabulary_task

    sync_row = AthenaVocabularySync.objects.create(
        source_url='https://example.test/athena', outcome='queued',
        started_at=timezone.now(),
    )

    def command(name, **options):
        assert name == 'sync_athena_vocabulary'
        assert options['sync_id'] == sync_row.pk
        AthenaVocabularySync.objects.filter(pk=sync_row.pk).update(
            outcome='applied', completed_at=timezone.now(),
        )

    monkeypatch.setattr(management, 'call_command', command)
    result = sync_athena_vocabulary_task.run(sync_row.pk)

    sync_row.refresh_from_db()
    assert sync_row.outcome == 'applied'
    assert result == {'sync_id': sync_row.pk, 'outcome': 'applied'}


@pytest.mark.django_db
def test_worker_records_failures_that_escape_before_the_command_receipt(monkeypatch):
    from django.core import management
    from omop_core.tasks import sync_athena_vocabulary_task

    sync_row = AthenaVocabularySync.objects.create(
        source_url='https://example.test/athena', outcome='queued',
        started_at=timezone.now(),
    )
    monkeypatch.setattr(
        management, 'call_command', Mock(side_effect=RuntimeError('command unavailable')),
    )

    with pytest.raises(RuntimeError, match='command unavailable'):
        sync_athena_vocabulary_task.run(sync_row.pk)

    sync_row.refresh_from_db()
    assert sync_row.outcome == 'failed'
    assert sync_row.failure_reason == 'command unavailable'
    assert sync_row.completed_at is not None
