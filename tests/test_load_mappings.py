import json

import pytest
from django.core.management import call_command

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory, VocabularyFactory


pytestmark = pytest.mark.django_db


def test_load_mappings_imports_approved_rows_and_skips_proposals(tmp_path):
    target = ConceptFactory(
        concept_id=991_001,
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        concept_code='1234-5',
        domain_id='Measurement',
    )
    artifact = tmp_path / 'mappings.json'
    artifact.write_text(json.dumps({
        'mappings': [
            {
                'source_vocabulary_id': 'HK-Labs',
                'source_code': 'example lab',
                'source_code_description': 'Example lab',
                'target_vocabulary_id': 'LOINC',
                'target_concept_code': '1234-5',
                'domain_id': 'Measurement',
                'status': 'approved',
                'origins': ['HK-Labs'],
            },
            {
                'source_vocabulary_id': 'HK-Labs',
                'source_code': 'ambiguous lab',
                'source_code_description': 'Ambiguous lab',
                'target_vocabulary_id': 'LOINC',
                'target_concept_code': '1234-5',
                'domain_id': 'Measurement',
                'status': 'proposed',
                'origins': ['HK-Labs'],
            },
        ],
    }))

    call_command('load_mappings', artifact=str(artifact))

    mapping = SourceCodeConceptMapping.objects.get(source_code='example lab')
    assert mapping.target_concept_id == target.concept_id
    assert mapping.status == 'approved'
    assert mapping.origin_system == 'HK-Labs'
    assert not SourceCodeConceptMapping.objects.filter(source_code='ambiguous lab').exists()


def test_load_mappings_is_idempotent(tmp_path):
    ConceptFactory(
        concept_id=991_002,
        vocabulary=VocabularyFactory(vocabulary_id='LOINC'),
        concept_code='1234-6',
        domain_id='Measurement',
    )
    artifact = tmp_path / 'mappings.json'
    artifact.write_text(json.dumps({'mappings': [{
        'source_vocabulary_id': 'HK-Labs',
        'source_code': 'idempotent lab',
        'target_vocabulary_id': 'LOINC',
        'target_concept_code': '1234-6',
        'domain_id': 'Measurement',
        'status': 'approved',
        'origins': ['HK-Labs'],
    }]}))

    call_command('load_mappings', artifact=str(artifact))
    call_command('load_mappings', artifact=str(artifact))

    assert SourceCodeConceptMapping.objects.filter(source_code='idempotent lab').count() == 1
