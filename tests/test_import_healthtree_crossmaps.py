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
        '| ICD10 | A01 | SNOMED | 111 | Condition | proposed | HT-Next, HT-One | 1 |\n'
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


def test_importer_skips_retired_standard_target_concepts(tmp_path):
    """Historical HealthTree targets must not become curator proposals."""
    retired = _concept(333, 'RETIRED-333', 'SNOMED', 'Condition')
    retired.invalid_reason = 'D'
    retired.save(update_fields=['invalid_reason'])
    artifact = tmp_path / 'HealthTree_Code_To_Concept_Mapping.md'
    artifact.write_text(
        '# HealthTree Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| ICD10 | A01 | SNOMED | RETIRED-333 | Condition | proposed | HT-One | 1 |\n'
    )
    call_command('import_healthtree_crossmaps', f'--artifact={artifact}', stdout=StringIO())
    mapping = SourceCodeConceptMapping.objects.get(source_code='A01')
    assert mapping.target_concept_id is None
    assert mapping.destination_candidates.get().target_concept_id == retired.pk


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


def _artifact(tmp_path, targets=('111', '222', '999')):
    artifact = tmp_path / 'all-destinations.json'
    artifact.write_text(json.dumps({'mappings': [{
        'source_vocabulary_id': 'ICD10', 'source_code': 'A01',
        'domain_id': 'Condition', 'status': 'proposed', 'origins': ['HT-One'],
        'target_vocabulary_id': 'SNOMED', 'target_concept_code': targets[0],
        'candidates': [{'target_vocabulary_id': 'SNOMED', 'target_concept_code': code,
                        'origins': ['HT-One']} for code in targets],
    }]}))
    return artifact


def test_all_candidates_survive_reload_without_selecting_a_winner(tmp_path):
    from omop_core.models import MappingDestinationCandidate
    _concept(111, '111', 'SNOMED', 'Condition')
    _concept(222, '222', 'SNOMED', 'Condition')
    artifact = _artifact(tmp_path)
    out = StringIO()
    for _ in range(2):
        call_command('import_healthtree_crossmaps', artifact=str(artifact),
                     skip_suggest_embeddings=True, stdout=out)
    mapping = SourceCodeConceptMapping.objects.get(source_code='A01')
    assert mapping.target_concept_id is None
    assert mapping.status == 'proposed'
    assert MappingDestinationCandidate.objects.count() == 3
    assert mapping.destination_candidates.get(target_concept_code='999').target_concept_id is None
    assert 'unavailable targets 1' in out.getvalue()


def test_reload_preserves_curator_selection_and_resolves_newly_loaded_targets(tmp_path):
    _concept(111, '111', 'SNOMED', 'Condition')
    chosen = _concept(222, '222', 'SNOMED', 'Condition')
    mapping = SourceCodeConceptMapping.objects.create(
        source_vocabulary_id='ICD10', source_code='A01', target_concept=chosen,
        status='approved', notes='Curator decision', origin_system='curator', occurrence_count=42,
    )
    artifact = _artifact(tmp_path)
    call_command('import_healthtree_crossmaps', artifact=str(artifact), skip_suggest_embeddings=True, stdout=StringIO())
    _concept(999, '999', 'SNOMED', 'Condition')
    call_command('import_healthtree_crossmaps', artifact=str(artifact), skip_suggest_embeddings=True, stdout=StringIO())
    mapping.refresh_from_db()
    assert (mapping.target_concept_id, mapping.status, mapping.notes, mapping.occurrence_count) == (222, 'approved', 'Curator decision', 42)
    assert mapping.destination_candidates.count() == 3
    assert mapping.destination_candidates.get(target_concept_code='999').target_concept_id == 999


def test_dry_run_does_not_write_candidates_or_mappings(tmp_path):
    from omop_core.models import MappingDestinationCandidate
    call_command('import_healthtree_crossmaps', artifact=str(_artifact(tmp_path)), dry_run=True, stdout=StringIO())
    assert not SourceCodeConceptMapping.objects.exists()
    assert not MappingDestinationCandidate.objects.exists()


def test_markdown_cannot_silently_drop_alternatives(tmp_path):
    from django.core.management.base import CommandError
    artifact = tmp_path / 'incomplete.md'
    artifact.write_text('| ICD10 | A01 | SNOMED | 111 | Condition | proposed | HT-One | 2 |\n')
    with pytest.raises(CommandError, match='Markdown omits alternative'):
        call_command('import_healthtree_crossmaps', artifact=str(artifact), stdout=StringIO())
