"""Source codes are named from Athena, then UMLS, and a curator's text is never touched (#1464)."""
from importlib import import_module
from itertools import count

import pytest
from django.apps import apps
from django.core.management import call_command

from omop_core.mapping.code_resolution import _record_proposal
from omop_core.models import (
    Concept, ConditionOccurrence, SourceCodeConceptMapping,
    UmlsConcept, UmlsRelease, UmlsSourceCode,
)
from omop_core.services.source_descriptions import (
    backfill_source_descriptions, describe_source_code,
)
from tests.factories import (
    ConceptClassFactory, ConceptFactory, DomainFactory, PersonFactory, VocabularyFactory,
)

pytestmark = pytest.mark.django_db

_cui = count(1)


def run(**kwargs):
    return backfill_source_descriptions(SourceCodeConceptMapping, Concept, UmlsSourceCode, **kwargs)


@pytest.fixture()
def condition_domain():
    return DomainFactory(domain_id='Condition')


@pytest.fixture()
def finding_class():
    return ConceptClassFactory(concept_class_id='Clinical Finding')


def concept(vocabulary_id, code, name, domain, klass, **kwargs):
    vocab = VocabularyFactory(vocabulary_id=vocabulary_id, vocabulary_name=vocabulary_id)
    kwargs.setdefault('standard_concept', 'S')
    return ConceptFactory(concept_name=name, concept_code=code, vocabulary=vocab,
                          domain=domain, concept_class=klass, **kwargs)


def mapping(vocabulary_id, code, description='', **kwargs):
    return SourceCodeConceptMapping.objects.create(
        source_vocabulary_id=vocabulary_id, source_code=code,
        source_code_description=description, status='proposed', **kwargs,
    )


def umls_atoms(root_source, code, *atoms):
    """atoms: (name, term_type, is_preferred) tuples for one code; callable repeatedly."""
    release, _ = UmlsRelease.objects.get_or_create(release_version='2025AA')
    cui = UmlsConcept.objects.create(cui=f'C{next(_cui):07d}', preferred_name=atoms[0][0],
                                     release=release)
    for name, term_type, preferred in atoms:
        UmlsSourceCode.objects.create(concept=cui, root_source=root_source, code=code,
                                      term_type=term_type, name=name, is_preferred=preferred)


# --- Athena tier -------------------------------------------------------------

def test_athena_fills_by_vocabulary_and_code(condition_domain, finding_class):
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    row = mapping('SNOMED', '109989006')

    counts = run()

    row.refresh_from_db()
    assert row.source_code_description == 'Multiple myeloma'
    assert counts['athena'] == 1 and counts['still_unnamed'] == 0


def test_existing_description_is_never_overwritten(condition_domain, finding_class):
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    row = mapping('SNOMED', '109989006', description='Curator wrote this')

    run()

    row.refresh_from_db()
    assert row.source_code_description == 'Curator wrote this'


def test_description_that_merely_repeats_the_code_counts_as_unnamed(condition_domain, finding_class):
    """FHIR sync stores the code itself when a Coding has no display."""
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    row = mapping('SNOMED', '109989006', description='109989006')

    run()

    row.refresh_from_db()
    assert row.source_code_description == 'Multiple myeloma'


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


# --- UMLS tier ---------------------------------------------------------------

def test_umls_prefers_the_pt_over_a_preferred_abbreviation():
    """MRCONSO's ISPREF is per string: on staging 2,918 CPT codes flag the
    abbreviation and leave the full PT unflagged."""
    umls_atoms('CPT', '00174',
               ('ANES NTRORL EXC RTRPHRNG TUM', 'AB', True),
               ('Anesthesia for intraoral procedures, including biopsy; '
                'excision of retropharyngeal tumor', 'PT', False))
    row = mapping('CPT4', '00174')

    counts = run()

    row.refresh_from_db()
    assert row.source_code_description.startswith('Anesthesia for intraoral')
    assert row.umls_source_name.startswith('Anesthesia for intraoral')
    assert counts['umls'] == 1


def test_umls_picks_one_atom_per_row_across_codes():
    umls_atoms('MDR', '10028533', ('Nausea', 'PT', True), ('Nauseous', 'SY', False))
    umls_atoms('MDR', '10047700', ('Vomiting', 'PT', True))
    nausea, vomiting = mapping('MedDRA', '10028533'), mapping('MedDRA', '10047700')

    counts = run()

    nausea.refresh_from_db(); vomiting.refresh_from_db()
    assert (nausea.source_code_description, vomiting.source_code_description) == ('Nausea', 'Vomiting')
    assert counts['umls'] == 2


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
    assert counts == {'athena': 0, 'athena_alias': 0, 'umls': 0, 'still_unnamed': 1}


def test_min_id_limits_every_pass_to_newer_rows(condition_domain, finding_class):
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    concept('SNOMED', '26643006', 'Oral', condition_domain, finding_class)
    umls_atoms('MDR', '10028533', ('Nausea', 'PT', True))
    older = mapping('SNOMED', '109989006')
    older_umls = mapping('MedDRA', '10028533')
    newer = mapping('SNOMED', '26643006')

    counts = run(min_id=older_umls.id)

    older.refresh_from_db(); older_umls.refresh_from_db(); newer.refresh_from_db()
    assert older.source_code_description == '' and older_umls.source_code_description == ''
    assert newer.source_code_description == 'Oral'
    assert counts == {'athena': 1, 'athena_alias': 0, 'umls': 0, 'still_unnamed': 0}


# --- single-code lookup and the ingest choke point ----------------------------

def test_describe_source_code_walks_the_same_tiers(condition_domain, finding_class):
    concept('ICD10CM', 'C85.90', 'Non-Hodgkin lymphoma, unspecified, unspecified site',
            condition_domain, finding_class)
    umls_atoms('CPT', '00174', ('ANES NTRORL EXC RTRPHRNG TUM', 'AB', True),
               ('Anesthesia for intraoral procedures', 'PT', False))

    assert describe_source_code('ICD10', 'C85.90').startswith('Non-Hodgkin lymphoma')
    assert describe_source_code('CPT4', '00174') == 'Anesthesia for intraoral procedures'
    assert describe_source_code('SNOMED', 'nope') == ''
    assert describe_source_code('', '00174') == ''


def test_record_proposal_names_a_row_born_without_a_display(condition_domain, finding_class):
    concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)

    # FHIR sync passes the code as the text when a Coding has no display.
    _record_proposal(source_vocabulary_id='SNOMED', source_code='109989006',
                     source_text='109989006', concept=None, omop_table='condition',
                     source_system='test')
    # An importer that passes a real display keeps it.
    _record_proposal(source_vocabulary_id='SNOMED', source_code='26643006',
                     source_text='Oral route', concept=None, omop_table='condition',
                     source_system='test')

    by_code = {m.source_code: m.source_code_description
               for m in SourceCodeConceptMapping.objects.all()}
    assert by_code == {'109989006': 'Multiple myeloma', '26643006': 'Oral route'}


# --- migration entry point and the enqueue hook ------------------------------

def test_migration_0244_runs_the_backfill(condition_domain, finding_class):
    concept('LOINC', '2345-7', 'Glucose [Mass/volume] in Serum', condition_domain, finding_class)
    umls_atoms('MDR', '10028533', ('Nausea', 'PT', True))
    loinc, meddra = mapping('LOINC', '2345-7'), mapping('MedDRA', '10028533')

    import_module('omop_core.migrations.0244_backfill_source_descriptions').backfill(apps, None)

    loinc.refresh_from_db(); meddra.refresh_from_db()
    assert loinc.source_code_description == 'Glucose [Mass/volume] in Serum'
    assert meddra.source_code_description == 'Nausea'


def test_enqueue_names_only_the_rows_it_lands(condition_domain, finding_class):
    """A landed row is named; a pre-existing unnamed row is left to the migration."""
    zero = Concept.objects.filter(concept_id=0).first() or concept(
        'None', 'No matching concept', 'No matching concept', condition_domain, finding_class,
        concept_id=0, standard_concept=None,
    )
    myeloma = concept('SNOMED', '109989006', 'Multiple myeloma', condition_domain, finding_class)
    ConditionOccurrence.objects.create(
        condition_occurrence_id=1, person=PersonFactory(), condition_concept=zero,
        condition_source_concept=myeloma, condition_source_value='109989006',
        condition_start_date='2022-06-01', condition_type_concept=zero,
    )
    untouched = mapping('SNOMED', '26643006')
    concept('SNOMED', '26643006', 'Oral', condition_domain, finding_class)

    call_command('enqueue_unmapped_source_codes', table=['condition'], min_occurrences=1)

    landed = SourceCodeConceptMapping.objects.get(source_code='109989006')
    untouched.refresh_from_db()
    assert landed.source_code_description == 'Multiple myeloma'
    assert untouched.source_code_description == ''
