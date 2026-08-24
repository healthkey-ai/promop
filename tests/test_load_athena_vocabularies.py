from io import StringIO
import zipfile

import pytest
from django.core.management import CommandError

from omop_core.management.commands.load_athena_vocabularies import (
    Command,
    DEFAULT_GDRIVE_URL,
    REQUIRED_CLINICAL_VOCABULARIES,
)
from tests.factories import (
    ConceptFactory, MeasurementFactory, PersonFactory, VocabularyFactory,
)


pytestmark = pytest.mark.django_db


def test_gdrive_option_defaults_to_shared_vocabulary_folder():
    parser = Command().create_parser('manage.py', 'load_athena_vocabularies')

    options = parser.parse_args(['--gdrive'])

    assert options.gdrive == DEFAULT_GDRIVE_URL


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
        )


def test_required_clinical_vocabulary_verification_reports_loaded_counts():
    for vocabulary_id in REQUIRED_CLINICAL_VOCABULARIES:
        ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id=vocabulary_id))

    output = StringIO()
    Command(stdout=output)._verify_required_clinical_vocabularies()

    text = output.getvalue()
    assert 'verified required clinical vocabularies' in text
    for vocabulary_id in REQUIRED_CLINICAL_VOCABULARIES:
        assert vocabulary_id in text


def test_required_clinical_vocabulary_verification_fails_for_partial_load():
    ConceptFactory(vocabulary=VocabularyFactory(vocabulary_id='LOINC'))

    with pytest.raises(CommandError, match='ICD10CM.*RxNorm.*SNOMED'):
        Command(stdout=StringIO())._verify_required_clinical_vocabularies()


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
