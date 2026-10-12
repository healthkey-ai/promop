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


def test_importer_creates_mappings_from_markdown(tmp_path):
    _concept(111, '8716-3', 'LOINC', 'Measurement')
    _concept(222, '85354-9', 'LOINC', 'Measurement')
    artifact = tmp_path / 'ht-fhir-code-concept-mapping.md'
    artifact.write_text(
        '# CureHub FHIR Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| LOINC | 8716-3 | LOINC | 8716-3 |  | proposed | HT-FHIR | 163479 |\n'
        '| LOINC | 85354-9 | LOINC | 85354-9 |  | proposed | HT-FHIR | 43699 |\n'
    )
    call_command('import_htfhir_crossmaps', f'--artifact={artifact}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 2
    mapping = SourceCodeConceptMapping.objects.get(source_code='8716-3')
    assert mapping.status == 'proposed'
    assert mapping.origin_system == 'HT-FHIR'
    assert mapping.source == 'HT-FHIR'
    assert mapping.origin == 'import'
    assert mapping.occurrence_count == 163479


def test_importer_is_idempotent(tmp_path):
    _concept(111, '8716-3', 'LOINC', 'Measurement')
    artifact = tmp_path / 'ht-fhir-code-concept-mapping.md'
    artifact.write_text(
        '# CureHub FHIR Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| LOINC | 8716-3 | LOINC | 8716-3 |  | proposed | HT-FHIR | 163479 |\n'
    )
    call_command('import_htfhir_crossmaps', f'--artifact={artifact}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 1
    # Second run should not create duplicates
    call_command('import_htfhir_crossmaps', f'--artifact={artifact}', stdout=StringIO())
    assert SourceCodeConceptMapping.objects.count() == 1


def test_importer_skips_unmapped_rows(tmp_path):
    _concept(111, '8716-3', 'LOINC', 'Measurement')
    artifact = tmp_path / 'ht-fhir-code-concept-mapping.md'
    artifact.write_text(
        '# CureHub FHIR Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| LOINC | 8716-3 | LOINC | 8716-3 |  | proposed | HT-FHIR | 163479 |\n'
        '| urn:oid:1.2.3 | 12345 |  |  |  | proposed | HT-FHIR | 500 |\n'
        '| urn:oid:1.2.4 | 67890 |  |  |  | proposed | HT-FHIR | 300 |\n'
    )
    out = StringIO()
    call_command('import_htfhir_crossmaps', f'--artifact={artifact}', stdout=out)
    assert SourceCodeConceptMapping.objects.count() == 1
    assert 'unmapped (skipped) 2' in out.getvalue()


def test_importer_skips_missing_target_concepts(tmp_path):
    # No concepts created — target lookup will fail
    artifact = tmp_path / 'ht-fhir-code-concept-mapping.md'
    artifact.write_text(
        '# CureHub FHIR Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| LOINC | 8716-3 | LOINC | 8716-3 |  | proposed | HT-FHIR | 163479 |\n'
    )
    out = StringIO()
    call_command('import_htfhir_crossmaps', f'--artifact={artifact}', stdout=out)
    assert SourceCodeConceptMapping.objects.count() == 0
    assert 'missing target 1' in out.getvalue()


def test_importer_dry_run_does_not_write(tmp_path):
    _concept(111, '8716-3', 'LOINC', 'Measurement')
    artifact = tmp_path / 'ht-fhir-code-concept-mapping.md'
    artifact.write_text(
        '# CureHub FHIR Code-to-Concept Mapping\n\n'
        '| Source system | Source code | Target OMOP vocabulary | Target OMOP code | Domain | Status | Origins | Candidate targets |\n'
        '| --- | --- | --- | --- | --- | --- | --- | --- |\n'
        '| LOINC | 8716-3 | LOINC | 8716-3 |  | proposed | HT-FHIR | 163479 |\n'
    )
    out = StringIO()
    call_command('import_htfhir_crossmaps', f'--artifact={artifact}', '--dry-run', stdout=out)
    assert SourceCodeConceptMapping.objects.count() == 0
    assert 'Would create 1' in out.getvalue()
