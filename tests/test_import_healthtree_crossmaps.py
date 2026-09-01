"""Tests for importing the code-held HealthTree crosswalks into SCCM."""
import json
from io import StringIO

import pytest
from django.core.management import call_command

from omop_core.models import SourceCodeConceptMapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


def make_project(tmp_path, name='one'):
    root = tmp_path / name
    fhir_root = root / 'functions/main/firestore/apps/curehub'
    condition = fhir_root / 'FHIR/resourcesTypes/r4/Condition'
    code_systems = fhir_root / 'FHIR/codeSystems'
    adverse = fhir_root / 'medicalResources/_DocumentReferenceAI/linesOfTherapy/adverseEvents/_utils'
    for directory in (condition, code_systems, adverse):
        directory.mkdir(parents=True)
    (condition / '_icd10ToSnomedMappings.json').write_text(json.dumps({'A00': ['111'], 'A01': ['111', '222']}))
    (code_systems / 'cptToSnomedMap.json').write_text(json.dumps({
        '99213': {'cptCode': '99213', 'cptConceptId': '501', 'cptDescriptor': 'Office visit', 'snomedId': '222'},
    }))
    (code_systems / 'snomedToRxNormMap.json').write_text(json.dumps({'333': '444'}))
    (adverse / 'MDRToSnomed.json').write_text(json.dumps([
        {'mdr_code': '1000', 'mdr_name': 'Nausea', 'snomed_code': '111'},
    ]))
    return root


def concept(concept_id, code, vocabulary, domain):
    return ConceptFactory(
        concept_id=concept_id, concept_code=code,
        vocabulary=VocabularyFactory(vocabulary_id=vocabulary),
        domain=DomainFactory(domain_id=domain), standard_concept='S',
    )


def test_imports_unambiguous_crosswalks_with_project_origin(tmp_path):
    root = make_project(tmp_path)
    concept(111, '111', 'SNOMED', 'Condition')
    concept(222, '222', 'SNOMED', 'Procedure')
    concept(444, '444', 'RxNorm', 'Drug')
    ConceptFactory(
        concept_id=501, concept_code='99213', vocabulary=VocabularyFactory(vocabulary_id='CPT4'),
        domain=DomainFactory(domain_id='Procedure'), standard_concept=None,
    )

    call_command(
        'import_healthtree_crossmaps',
        '--project=one',
        f'--one-root={root}',
        stdout=StringIO(),
    )

    assert (
        SourceCodeConceptMapping.objects.get(
            source_vocabulary_id='ICD10', source_code='A00',
        ).origin_system == 'HT-One'
    )
    ambiguous_icd = SourceCodeConceptMapping.objects.get(
        source_vocabulary_id='ICD10', source_code='A01',
    )
    assert ambiguous_icd.target_concept_id == 111
    assert ambiguous_icd.status == 'proposed'
    cpt = SourceCodeConceptMapping.objects.get(source_vocabulary_id='CPT4', source_code='99213')
    assert cpt.target_concept_id == 222
    assert cpt.source_concept_id == 501
    assert cpt.omop_table == 'procedure'
    drug = SourceCodeConceptMapping.objects.get(source_vocabulary_id='SNOMED', source_code='333')
    assert drug.target_concept_id == 444
    assert drug.omop_table == 'drug_exposure'
    assert (
        SourceCodeConceptMapping.objects.get(
            source_vocabulary_id='MedDRA', source_code='1000',
        ).origin_system == 'HT-One'
    )


def test_next_uses_its_own_origin_and_dry_run_writes_nothing(tmp_path):
    root = make_project(tmp_path, 'next')
    concept(111, '111', 'SNOMED', 'Condition')
    concept(444, '444', 'RxNorm', 'Drug')
    out = StringIO()

    call_command('import_healthtree_crossmaps', '--project=next', f'--next-root={root}', '--dry-run', stdout=out)

    assert not SourceCodeConceptMapping.objects.exists()
    assert 'HT-Next ICD10→SNOMED: Would create 2' in out.getvalue()
