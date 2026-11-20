from io import StringIO

import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from omop_core.data_migrations.snomed_oid_v1 import OID
from omop_core.mapping.code_resolution import approved_mapping_for, resolve_source_code
from omop_core.models import SourceCodeConceptMapping as Mapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def target():
    return ConceptFactory(concept_code='234326005', vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
                          domain=DomainFactory(domain_id='Procedure'))


def resolve(vocabulary, code='234326005'):
    return resolve_source_code(source_vocabulary_id=vocabulary, source_code=code, omop_table='procedure')


def test_alias_uses_canonical_approved_curator_override(target):
    other = ConceptFactory(vocabulary=target.vocabulary, domain=target.domain)
    row = Mapping.objects.create(source_vocabulary_id='SNOMED', source_code=target.concept_code,
                                 status='approved', target_concept=other)
    assert resolve(OID) == (other, row)
    assert approved_mapping_for(OID, target.concept_code) == row
    assert Mapping.objects.count() == 1


def test_alias_direct_lookup_and_missing_code_store_canonical_rows(target):
    concept, row = resolve(OID)
    assert concept == target and row.status == 'approved'
    assert row.source_vocabulary_id == 'SNOMED'
    assert resolve('SNOMED') == (target, row)
    concept, gap = resolve(OID, 'missing')
    assert concept is None and gap.source_vocabulary_id == 'SNOMED' and gap.status == 'proposed'
    assert resolve('SNOMED', 'missing')[1].pk == gap.pk
    assert Mapping.objects.count() == 2


def test_proposal_is_not_bypassed_by_alias(target):
    row = Mapping.objects.create(source_vocabulary_id='SNOMED', source_code=target.concept_code,
                                 status='proposed', target_concept=target)
    concept, found = resolve(OID)
    assert concept is None and found.pk == row.pk
    row.refresh_from_db()
    assert row.status == 'proposed' and row.occurrence_count == 1


def test_retained_alias_conflict_is_not_bypassed(target):
    Mapping.objects.create(source_vocabulary_id='SNOMED', source_code=target.concept_code,
                           status='approved', target_concept=target)
    alias = Mapping.objects.create(source_vocabulary_id=OID, source_code=target.concept_code,
                                   status='proposed', target_concept=target)
    assert resolve(OID)[0] is None
    alias.refresh_from_db()
    assert alias.status == 'proposed'


def artifact(tmp_path):
    path = tmp_path / 'fhir.md'
    path.write_text(f'| {OID} | 234326005 | SNOMED | 234326005 | Procedure | proposed | HT-FHIR | 7 |\n'
                    '| SNOMED | 234326005 | SNOMED | 234326005 | Procedure | proposed | HT-FHIR | 7 |\n')
    return path


def test_import_alias_does_not_recreate_duplicate_or_demote_approval(target, tmp_path):
    existing = Mapping.objects.create(source_vocabulary_id='SNOMED', source_code=target.concept_code,
                                      target_concept=target, status='approved')
    path = artifact(tmp_path)
    for _ in range(2):
        call_command('import_htfhir_crossmaps', artifact=str(path), stdout=StringIO())
    existing.refresh_from_db()
    assert Mapping.objects.count() == 1 and existing.status == 'approved'


def test_import_new_alias_uses_one_canonical_identity(target, tmp_path):
    call_command('import_htfhir_crossmaps', artifact=str(artifact(tmp_path)), stdout=StringIO())
    row = Mapping.objects.get()
    assert row.source_vocabulary_id == 'SNOMED' and row.target_concept_id == target.pk
    assert row.occurrence_count == 7


def test_api_creation_normalizes_alias_and_rejects_duplicate(target):
    from patient_portal.models import Identity
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='oid-admin@example.test', is_staff=True))
    payload = dict(source_vocabulary_id=OID, source_code=target.concept_code,
                   destination_concept_id=target.pk, domain_id='Procedure')
    response = client.post('/api/v1/code-mappings/', payload, format='json')
    assert response.status_code == 201, response.data
    assert Mapping.objects.get().source_vocabulary_id == 'SNOMED'
    response = client.post('/api/v1/code-mappings/', payload, format='json')
    assert response.status_code == 400, response.data
    assert Mapping.objects.count() == 1


@pytest.mark.parametrize('command', ['import_healthtree_crossmaps', 'load_mappings'])
def test_other_crossmap_loaders_normalize_oid_and_preserve_approval(target, tmp_path, command):
    import json
    path = tmp_path / 'mappings.json'
    path.write_text(json.dumps({'mappings': [dict(
        source_vocabulary_id=OID, source_code=target.concept_code,
        target_vocabulary_id='SNOMED', target_concept_code=target.concept_code,
        domain_id='Procedure', status='approved', origins=['HT-FHIR'],
    )]}))
    for _ in range(2):
        call_command(command, artifact=str(path), stdout=StringIO())
    row = Mapping.objects.get()
    assert row.source_vocabulary_id == 'SNOMED'
    assert row.target_concept_id == target.pk and row.status == 'approved'


def test_lookup_api_preserves_requested_keys_for_both_spellings(target):
    from patient_portal.models import Identity
    client = APIClient()
    client.force_authenticate(Identity.objects.create_user(email='oid-lookup@example.test', is_staff=True))
    response = client.post('/api/v1/code-mappings/lookup/', {'codes': [
        dict(source_vocabulary_id=vocab, source_code=target.concept_code, omop_table='procedure')
        for vocab in [OID, 'SNOMED']
    ]}, format='json')
    assert response.status_code == 200, response.data
    results = response.data['mappings']
    assert set(results) == {f'{vocab}|234326005' for vocab in [OID, 'SNOMED']}
    assert {row['mapping_id'] for row in results.values()} == {Mapping.objects.get().pk}
    assert all(row['resolved'] for row in results.values())
