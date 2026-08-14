from io import StringIO

import pytest
from django.core.management import CommandError

from omop_core.management.commands.load_athena_vocabularies import (
    Command,
    REQUIRED_CLINICAL_VOCABULARIES,
)
from tests.factories import ConceptFactory, VocabularyFactory


pytestmark = pytest.mark.django_db


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
