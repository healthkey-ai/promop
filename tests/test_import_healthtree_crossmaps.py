import json
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def _concept(concept_id, code, vocabulary, domain):
    return ConceptFactory(
        concept_id=concept_id, concept_code=code,
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary),
        domain=DomainFactory(domain_id=domain), standard_concept='S',
    )


def test_importer_reads_reviewable_markdown_in_batches(tmp_path):
    _concept(111, '111', 'SNOMED', 'Condition')
    _concept(222, '222', 'SNOMED', 'Procedure')
    artifact = tmp_path / 'HealthTree_Code_To_Concept_Mapping.md'
    artifact.write_text(
        '# HealthTree Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| ICD10 | A01 | SNOMED | 111 | Condition | proposed | HT-Next, HT-One | 2 |\n'
        '| CPT4 | 99213 | SNOMED | 222 | Procedure | approved | HT-One | 1 |\n'
    )
    call_command('import_healthtree_crossmaps', f'--artifact={artifact}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 2
    mapping = SourceCodeConceptMapping.objects.get(source_code='A01')
    assert mapping.status == 'proposed'
    assert mapping.origin_system == 'HT-One'
    assert mapping.source == 'HT-One'
    call_command('import_healthtree_crossmaps', f'--artifact={artifact}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 2


def test_builder_records_frequency_and_project_agreement(tmp_path):
    for name, targets in [('one', ['111', '222']), ('next', ['111'])]:
        root = tmp_path / name / 'functions/main/firestore/apps/curehub'
        condition = root / 'FHIR/resourcesTypes/r4/Condition'
        systems = root / 'FHIR/codeSystems'
        adverse = root / 'medicalResources/_DocumentReferenceAI/linesOfTherapy/adverseEvents/_utils'
        for directory in (condition, systems, adverse):
            directory.mkdir(parents=True)
        (condition / '_icd10ToSnomedMappings.json').write_text(json.dumps({'A01': targets}))
        (systems / 'cptToSnomedMap.json').write_text('{}')
        (systems / 'snomedToRxNormMap.json').write_text('{}')
        (adverse / 'MDRToSnomed.json').write_text('[]')
    artifact = tmp_path / 'artifact.json'
    markdown = tmp_path / 'artifact.md'
    call_command('build_healthtree_crossmap_artifact', f'--one-root={tmp_path / "one"}', f'--next-root={tmp_path / "next"}', f'--output={artifact}', f'--markdown-output={markdown}')
    row = json.loads(artifact.read_text())['mappings'][0]
    assert row['target_concept_code'] == '111'
    assert row['status'] == 'proposed'
    assert row['candidates'][0]['occurrences'] == 2
    assert 'ICD10 | A01 | SNOMED | 111' in markdown.read_text()
