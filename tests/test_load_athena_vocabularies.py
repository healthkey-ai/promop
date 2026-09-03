from io import StringIO
import zipfile

import pytest
from django.core.management import CommandError

from omop_core.management.commands.load_athena_vocabularies import (
    Command,
    DEFAULT_GDRIVE_URL,
    REQUIRED_CLINICAL_VOCABULARIES,
    _cache_umls_release,
    _resolve_umls_release,
)
from tests.factories import (
    ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory,
)


def test_gdrive_option_defaults_to_shared_vocabulary_folder():
    parser = Command().create_parser('manage.py', 'load_athena_vocabularies')

    options = parser.parse_args(['--gdrive'])

    assert options.gdrive == DEFAULT_GDRIVE_URL


def test_umls_options_default_to_automatic_opt_in():
    parser = Command().create_parser('manage.py', 'load_athena_vocabularies')

    options = parser.parse_args(['--path', '/tmp/vocab'])

    assert options.umls_release_url is None
    assert options.skip_umls_cache is False


def test_resolve_current_umls_release_uses_nlm_release_endpoint():
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{
                'releaseVersion': '2026AA',
                'releaseDate': '2026-05-03',
                'downloadUrl': 'https://download.nlm.nih.gov/umls/kss/2026AA/umls-2026AA-full.zip',
            }]

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return Response()

    session = Session()
    release = _resolve_umls_release(session=session)

    assert release == {
        'release_url': 'https://download.nlm.nih.gov/umls/kss/2026AA/umls-2026AA-full.zip',
        'release_version': '2026AA',
        'release_date': '2026-05-03',
    }
    assert session.calls[0][1]['params'] == {
        'releaseType': 'umls-full-release', 'current': 'true',
    }


def test_cache_umls_release_downloads_valid_rrf_archive_and_reuses_it(tmp_path):
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('2026AA/META/MRCONSO.RRF', 'C0000005|ENG|P|L0000005|PF|S0000005|Y|A0000005||||SNOMEDCT_US|PT|123|Example|||N|\n')
    payload = archive.read_bytes()

    class DownloadResponse:
        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            assert chunk_size == 1024 * 1024
            yield payload

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            return DownloadResponse()

    release_url = 'https://download.nlm.nih.gov/umls/kss/2026AA/umls-2026AA-full.zip'
    session = Session()
    metadata = _cache_umls_release(
        api_key='not-a-real-key', cache_dir=tmp_path / 'cache',
        release_url=release_url, session=session,
    )

    assert metadata['release_version'] == '2026AA'
    assert metadata['archive_name'] == 'umls-2026AA-full.zip'
    assert metadata['sha256']
    assert metadata['cached'] is False
    assert session.calls[0][1]['params'] == {
        'url': release_url, 'apiKey': 'not-a-real-key',
    }

    reused = _cache_umls_release(
        api_key='not-a-real-key', cache_dir=tmp_path / 'cache',
        release_url=release_url, session=session,
    )
    assert reused['cached'] is True
    assert len(session.calls) == 1


def test_archive_source_extracts_vocabulary_zip(tmp_path):
    from omop_core.management.commands.load_athena_vocabularies import _extract_vocabulary_archive

    archive = tmp_path / 'athena.zip'
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('vocabulary_download/CONCEPT.csv', 'concept_id\tconcept_name\n')

    base = _extract_vocabulary_archive(
        archive, tmp_path / 'extracted', lambda msg: None
    )

    assert (tmp_path / 'extracted' / 'vocabulary_download' / 'CONCEPT.csv').exists()
    assert base == str(tmp_path / 'extracted' / 'vocabulary_download')


def test_loader_requires_exactly_one_source():
    cmd = Command(stdout=StringIO())

    with pytest.raises(CommandError, match='exactly one of --path, --archive, --bucket, or --gdrive'):
        cmd.handle(
            path='/tmp/vocab',
            archive=None,
            bucket='ctomop-staging-vocab',
            gdrive=None,
            replace=False,
            dry_run=True,
            skip_clinical_vocabulary_verification=False,
            concepts_only=False,
            umls_release_url=None,
            skip_umls_cache=False,
        )


@pytest.mark.django_db
def test_required_clinical_vocabulary_verification_reports_loaded_counts():
    for vocabulary_id in REQUIRED_CLINICAL_VOCABULARIES:
        ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id=vocabulary_id))

    output = StringIO()
    Command(stdout=output)._verify_required_clinical_vocabularies()

    text = output.getvalue()
    assert 'verified required clinical vocabularies' in text
    for vocabulary_id in REQUIRED_CLINICAL_VOCABULARIES:
        assert vocabulary_id in text


@pytest.mark.django_db
def test_required_clinical_vocabulary_verification_fails_for_partial_load():
    ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='LOINC'))

    with pytest.raises(CommandError, match='ICD10CM.*RxNorm.*SNOMED'):
        Command(stdout=StringIO())._verify_required_clinical_vocabularies()


@pytest.mark.django_db
def test_replace_removes_stale_concepts_without_removing_patients():
    """A replacement detaches stale concept links but retains patient rows."""
    stale = ConceptFactory(concept_id=998_001, concept_code='STALE-998001')
    person = PersonFactory(gender_concept=stale)
    measurement = MeasurementFactory(person=person, measurement_concept=stale)
    cmd = Command(stdout=StringIO())
    cmd._replace_tracking = True
    cmd._create_incoming_concept_table()
    cmd._record_incoming_concept_ids([999_001])
    cmd._seed_concept_zero()

    cmd._remove_stale_concepts()

    person.refresh_from_db()
    measurement.refresh_from_db()
    assert person.gender_concept_id is None
    assert type(person).objects.filter(person_id=person.person_id).exists()
    assert measurement.measurement_concept_id == 0
    assert type(measurement).objects.filter(
        measurement_id=measurement.measurement_id
    ).exists()
    assert not type(stale).objects.filter(concept_id=stale.concept_id).exists()
