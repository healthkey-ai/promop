"""Athena ZIP loads must use bounded scratch space and preserve CSV semantics."""

import csv
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import gdown
import pytest
from django.core.management import call_command, CommandError

from omop_core.management.commands import load_athena_vocabularies as loader
from tests.factories import ConceptFactory, VocabularyFactory


@pytest.mark.parametrize('prefix', ['', 'nested/export/', './export/'])
def test_zip_stream_can_be_reopened_without_extraction(tmp_path, monkeypatch, prefix):
    def no_extraction(*args, **kwargs):
        pytest.fail('Athena archives must not be extracted')

    monkeypatch.setattr(zipfile.ZipFile, 'extract', no_extraction)
    monkeypatch.setattr(zipfile.ZipFile, 'extractall', no_extraction)
    archive = tmp_path / 'athena.zip'
    row = '1\t"A multiline\nconcept with é and a tab\tinside"\r\n'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(prefix + 'CONCEPT.csv', 'id\tname\r\n' + row * 50_000)
        zf.writestr(prefix + 'UNUSED.csv', 'unused\n' * 100_000)
    source = loader._VocabularyArchive(archive, lambda msg: None)
    for _ in range(2):
        with source.open('CONCEPT.csv') as stream:
            rows = csv.reader(stream, delimiter='\t')
            assert next(rows) == ['id', 'name']
            assert next(rows) == ['1', 'A multiline\nconcept with é and a tab\tinside']
            assert sum(1 for _ in rows) == 49_999
        assert stream.closed
    assert list(tmp_path.iterdir()) == [archive]


def test_missing_optional_member_is_reported_when_opened(tmp_path):
    archive = tmp_path / 'athena.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('CONCEPT.csv', 'id\n')
    source = loader._VocabularyArchive(archive, lambda msg: None)
    with pytest.raises(CommandError, match='VOCABULARY.csv'):
        source.open('VOCABULARY.csv')


@pytest.mark.parametrize('member', ['../CONCEPT.csv', '/CONCEPT.csv', 'OTHER.csv'])
def test_invalid_archive_layout_fails_before_loading(tmp_path, member):
    archive = tmp_path / 'athena.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr(member, 'id\n')
    with pytest.raises(CommandError):
        loader._VocabularyArchive(archive, lambda msg: None)
    assert list(tmp_path.iterdir()) == [archive]


def test_folder_download_fetches_only_one_selected_zip(tmp_path, monkeypatch):
    listing = Mock(return_value=[
        SimpleNamespace(id='other-zip', path='z-other.zip'),
        SimpleNamespace(id='selected-zip', path='a-athena.zip'),
        SimpleNamespace(id='large-csv', path='CONCEPT.csv'),
    ])
    download = Mock(return_value=str(tmp_path / 'athena-vocabulary.zip'))
    monkeypatch.setattr(gdown, 'download_folder', listing)
    monkeypatch.setattr(gdown, 'download', download)
    result = loader._download_gdrive_vocabulary(
        'https://drive.google.com/drive/folders/test', tmp_path, lambda msg: None,
    )
    assert listing.call_args.kwargs['skip_download'] is True
    assert listing.call_args.kwargs['quiet'] is True
    download.assert_called_once_with(
        id='selected-zip', output=str(result), quiet=False, use_cookies=False,
    )


def test_folder_resolution_exposes_stable_file_identity_without_download(monkeypatch):
    listing = Mock(return_value=[
        SimpleNamespace(id='selected-zip', path='athena.zip'),
    ])
    monkeypatch.setattr(gdown, 'download_folder', listing)
    download = Mock()
    monkeypatch.setattr(gdown, 'download', download)

    selected = loader._resolve_gdrive_vocabulary(
        'https://drive.google.com/drive/folders/test', lambda msg: None,
    )

    assert selected == {
        'id': 'selected-zip', 'name': 'athena.zip',
        'identity': 'gdrive-file:selected-zip:athena.zip',
    }
    download.assert_not_called()


def test_direct_file_download_does_not_list_a_folder(tmp_path, monkeypatch):
    listing = Mock()
    download = Mock(return_value=str(tmp_path / 'athena-vocabulary.zip'))
    monkeypatch.setattr(gdown, 'download_folder', listing)
    monkeypatch.setattr(gdown, 'download', download)
    url = 'https://drive.google.com/file/d/test/view'
    result = loader._download_gdrive_vocabulary(url, tmp_path, lambda msg: None)
    listing.assert_not_called()
    download.assert_called_once_with(url=url, output=str(result), quiet=False, use_cookies=False)


@pytest.mark.parametrize('listing', [None, []])
def test_invalid_folder_listing_does_not_download_files(tmp_path, monkeypatch, listing):
    monkeypatch.setattr(gdown, 'download_folder', Mock(return_value=listing))
    download = Mock()
    monkeypatch.setattr(gdown, 'download', download)
    with pytest.raises(CommandError):
        loader._download_gdrive_vocabulary(
            'https://drive.google.com/drive/folders/test', tmp_path, lambda msg: None,
        )
    download.assert_not_called()


@pytest.mark.parametrize('fail_download', [False, True])
def test_failed_load_cleans_its_downloads_only(tmp_path, monkeypatch, fail_download):
    unrelated = tmp_path / 'keep.zip'
    unrelated.write_text('operator-owned file')
    owned_dirs = []

    def download(url, download_dir, log):
        owned_dirs.append(download_dir)
        archive = download_dir / 'athena.zip'
        with zipfile.ZipFile(archive, 'w') as zf:
            zf.writestr('CONCEPT.csv', 'id\n')
        if fail_download:
            raise OSError('Download interrupted')
        return archive

    monkeypatch.setattr(loader, '_download_gdrive_vocabulary', download)
    monkeypatch.setattr(loader.Command, '_load_relationships', Mock(side_effect=CommandError('Load interrupted')))
    with pytest.raises((CommandError, OSError), match='interrupted'):
        call_command(
            'load_athena_vocabularies', gdrive='https://drive.google.com/drive/folders/test',
            skip_umls_cache=True, stdout=StringIO(),
        )
    assert owned_dirs and not owned_dirs[0].exists()
    assert unrelated.read_text() == 'operator-owned file'


@pytest.mark.django_db
def test_archive_can_be_scanned_then_loaded_into_postgres(tmp_path):
    existing = ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='LOINC'))
    archive = tmp_path / 'athena.zip'
    rows = StringIO()
    writer = csv.writer(rows, delimiter='\t')
    writer.writerow([
        'concept_id', 'concept_name', 'domain_id', 'vocabulary_id',
        'concept_class_id', 'standard_concept', 'concept_code',
        'valid_start_date', 'valid_end_date', 'invalid_reason',
    ])
    writer.writerow([
        999001, 'Streamed concept é', existing.domain_id, existing.vocabulary_id,
        existing.concept_class_id, 'S', 'STREAM-TEST', '20260101', '20991231', '',
    ])
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('export/CONCEPT.csv', rows.getvalue())
    command = loader.Command(stdout=StringIO())
    command._archive = loader._VocabularyArchive(archive, command._log)
    command._gcs_bucket = None
    command._direct = False
    assert command._load_concepts(dry_run=True) == 1
    assert command._load_concepts(dry_run=False) == 1
    concept = loader.Concept.objects.get(pk=999001)
    assert concept.concept_name == 'Streamed concept é'
    assert concept.vocabulary_id == 'LOINC'
    assert concept.valid_start_date.isoformat() == '2026-01-01'
    assert loader.Concept.objects.filter(pk=existing.pk).exists()
    assert list(tmp_path.iterdir()) == [archive]


@pytest.mark.django_db
@pytest.mark.parametrize('source', ['archive', 'gdrive'])
def test_full_dry_run_reads_every_table_without_extracting(tmp_path, monkeypatch, source):
    archive = tmp_path / 'athena.zip'
    headers = {
        'RELATIONSHIP': 'relationship_id\trelationship_name',
        'VOCABULARY': 'vocabulary_id\tvocabulary_name',
        'DOMAIN': 'domain_id\tdomain_name',
        'CONCEPT_CLASS': 'concept_class_id\tconcept_class_name',
        'CONCEPT': 'concept_id\tconcept_name\tdomain_id\tvocabulary_id\tconcept_class_id\tstandard_concept\tconcept_code\tvalid_start_date\tvalid_end_date\tinvalid_reason',
        'CONCEPT_RELATIONSHIP': 'concept_id_1\tconcept_id_2\trelationship_id\tvalid_start_date\tvalid_end_date\tinvalid_reason',
        'CONCEPT_ANCESTOR': 'ancestor_concept_id\tdescendant_concept_id\tmin_levels_of_separation\tmax_levels_of_separation',
        'CONCEPT_SYNONYM': 'concept_id\tconcept_synonym_name\tlanguage_concept_id',
        'DRUG_STRENGTH': 'drug_concept_id\tingredient_concept_id',
        'SOURCE_TO_CONCEPT_MAP': 'source_code\tsource_concept_id\tsource_vocabulary_id\tsource_code_description\ttarget_concept_id\ttarget_vocabulary_id\tvalid_start_date\tvalid_end_date\tinvalid_reason',
    }
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
        for table, header in headers.items():
            zf.writestr(f'export/{table}.csv', header + '\n')
    owned_dirs = []

    def download(url, download_dir, log):
        owned_dirs.append(download_dir)
        dest = download_dir / 'athena.zip'
        dest.write_bytes(archive.read_bytes())
        return dest

    monkeypatch.setattr(loader, '_download_gdrive_vocabulary', download)
    output = StringIO()
    call_command(
        'load_athena_vocabularies', **{source: str(archive)},
        dry_run=True, skip_umls_cache=True, stdout=output,
    )
    assert 'LOAD SUMMARY' in output.getvalue()
    for table in headers:
        assert f'Loading {table}.csv' in output.getvalue()
    assert list(tmp_path.iterdir()) == [archive]
    assert all(not path.exists() for path in owned_dirs)
