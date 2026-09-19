"""Source descriptions are filled from Athena, then UMLS, and never overwritten (#1464)."""
from importlib import import_module

import pytest
from django.apps import apps
from django.core.management import call_command

from omop_core.models import (
    SourceCodeConceptMapping, UmlsConcept, UmlsRelease, UmlsSourceCode,
)
from omop_core.services.source_descriptions import backfill_source_descriptions
from tests.factories import ConceptClassFactory, ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def run():
    from omop_core.models import Concept
    return backfill_source_descriptions(SourceCodeConceptMapping, Concept, UmlsSourceCode)


@pytest.fixture()
def condition_domain():
    return DomainFactory(domain_id='Condition')


@pytest.fixture()
def finding_class():
    return ConceptClassFactory(concept_class_id='Clinical Finding')


def concept(vocabulary_id, code, name, domain, klass):
    vocab = VocabularyFactory(vocabulary_id=vocabulary_id, vocabulary_name=vocabulary_id)
    return ConceptFactory(concept_name=name, concept_code=code, vocabulary=vocab,
                          domain=domain, concept_class=klass, standard_concept='S')


def mapping(vocabulary_id, code, description='', **kwargs):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=vocabulary_id, source_code=code,
        source_code_description=description, status='proposed', **kwargs,
    )


def umls_atoms(root_source, code, *atoms):
    """atoms: (name, term_type, is_preferred) tuples for one code."""
    release = UmlsRelease.objects.create(release_version='2025AA')
    cui = UmlsConcept.objects.create(cui='C0000001', preferred_name=atoms[0][0], release=release)
    for name, term_type, preferred in atoms:
        UmlsSourceCode.objects.create(concept=cui, root_source=root_source, code=code,
                                      term_type=term_type, name=name, is_preferred=preferred)


def test_athena_fills_by_vocabulary_and_code(condition_domain, finding_class):
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    row = mapping('SNOMED', '109989006')

    counts = run()

    row.refresh_from_db()
    assert row.source_code_description == 'Multiple myeloma'
    assert counts['athena'] == 1 and counts['still_empty'] == 0


def test_existing_description_is_never_overwritten(condition_domain, finding_class):
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    row = mapping('SNOMED', '109989006', description='Curator wrote this')

    run()

    row.refresh_from_db()
    assert row.source_code_description == 'Curator wrote this'


def test_snomed_oid_alias_resolves_to_snomed(condition_domain, finding_class):
    concept('SNOMED', '26643006', 'Oral', condition_domain, finding_class)
    row = mapping('urn:oid:2.16.840.1.113883.6.96', '26643006')

    counts = run()

    row.refresh_from_db()
    assert row.source_code_description == 'Oral'
    assert counts['athena_alias'] == 1


def test_icd10_code_present_only_as_icd10cm(condition_domain, finding_class):
    """HT-One's ICD-10 rows are ICD-10-CM codes; C85.90 has no WHO ICD-10 entry."""
    concept('ICD10CM', 'C85.90', 'Non-Hodgkin lymphoma, unspecified, unspecified site',
            condition_domain, finding_class)
    row = mapping('ICD10', 'C85.90')

    run()

    row.refresh_from_db()
    assert row.source_code_description.startswith('Non-Hodgkin lymphoma, unspecified')


def test_umls_fills_licensed_vocabularies_with_best_atom():
    umls_atoms('CPT', '93308',
               ('ECHO TRANSTHORC R-T 2D W/WO M-MODE REC F-UP/LMTD', 'AB', False),
               ('Echocardiography, transthoracic, real-time, follow-up or limited study', 'PT', True))
    row = mapping('CPT4', '93308')

    counts = run()

    row.refresh_from_db()
    assert row.source_code_description.startswith('Echocardiography, transthoracic')
    assert row.umls_source_name.startswith('Echocardiography, transthoracic')
    assert counts['umls'] == 1


def test_umls_covers_meddra():
    umls_atoms('MDR', '10028533', ('Nausea', 'PT', True))
    row = mapping('MedDRA', '10028533')

    run()

    row.refresh_from_db()
    assert row.source_code_description == 'Nausea'


def test_description_is_truncated_to_column_width():
    """Athena names fit by construction; UMLS atom names are unbounded text."""
    umls_atoms('CPT', '99999', ('x' * 300, 'PT', True))
    row = mapping('CPT4', '99999')

    run()

    row.refresh_from_db()
    assert len(row.source_code_description) == 255
    assert len(row.umls_source_name) == 300


def test_unresolvable_rows_are_counted_and_left_empty():
    row = mapping('SNOMED', 'no-such-code')
    blank_vocab = mapping('', 'free text')

    counts = run()

    row.refresh_from_db(); blank_vocab.refresh_from_db()
    assert row.source_code_description == '' and blank_vocab.source_code_description == ''
    assert counts == {'athena': 0, 'athena_alias': 0, 'umls': 0, 'still_empty': 1}


def test_migration_0244_runs_the_backfill(condition_domain, finding_class):
    concept('LOINC', '2345-7', 'Glucose [Mass/volume] in Serum', condition_domain, finding_class)
    row = mapping('LOINC', '2345-7')

    import_module('omop_core.migrations.0244_backfill_source_descriptions').backfill(apps, None)

    row.refresh_from_db()
    assert row.source_code_description == 'Glucose [Mass/volume] in Serum'


def test_enqueue_names_the_rows_it_lands(condition_domain, finding_class):
    """The hook in enqueue_unmapped_source_codes keeps the backlog from regrowing."""
    concept('RxNorm', '1234', 'Lenalidomide', condition_domain, finding_class)
    row = mapping('RxNorm', '1234')

    call_command('enqueue_unmapped_source_codes', min_occurrences=1)

    row.refresh_from_db()
    assert row.source_code_description == 'Lenalidomide'
