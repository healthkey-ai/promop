"""Frozen mapping recovery deploys offline and preserves curator decisions."""
import importlib
from types import SimpleNamespace
from collections import Counter

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext, override_settings

from omop_core.data_migrations.athena_recovery_v1 import reconcile
from omop_core.models import Concept, MappingDestinationCandidate, SourceCodeConceptMapping as Mapping
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory
from tests.test_athena_destinations import concept, queue

migration = importlib.import_module('omop_core.migrations.0253_reconcile_athena_icd10_mappings')


def payload(multiple=False):
    targets = [concept(201, 'SNOMED-201', 'SNOMED', 'S')]
    if multiple:
        targets.append(concept(202, 'SNOMED-202', 'SNOMED', 'S'))
    return {'schema_version': 1, 'mapping_count': 1, 'concept_count': len(targets), 'as_of': '2026-09-21',
        'concepts': {r['concept_id']: r for r in targets},
        'metadata': {
            'domain_id': {'Condition': dict(domain_name='Condition', domain_concept_id=19)},
            'vocabulary_id': {'SNOMED': dict(vocabulary_name='SNOMED CT', vocabulary_reference='https://snomed.org',
                vocabulary_version='test', vocabulary_concept_id=1)},
            'concept_class_id': {'Clinical Finding': dict(concept_class_name='Clinical Finding', concept_class_concept_id=2)},
        },
        'mappings': [dict(source_vocabulary_id='ICD10', lookup_vocabulary='ICD10CM', source_code='C91.10',
            source_concept_id=101, source_code_description='Test diagnosis', target_concept_ids=[int(r['concept_id']) for r in targets],
            tier='local directory', reference='https://athena.ohdsi.org/search-terms/terms/101')]}


def historical_apps():
    with override_settings(MIGRATION_MODULES={}):
        return MigrationLoader(connection).project_state([('omop_core', '0252_genomic_feature_recipes')]).apps


@pytest.mark.django_db
@pytest.mark.parametrize('multiple', [False, True])
def test_historical_loader_replaces_import_proposals_and_choices_idempotently(multiple):
    old = ConceptFactory()
    row = Mapping.objects.create(source_vocabulary_id='ICD10', source_code='C91.10', origin='import',
        origin_system='HT-One', target_concept=old, notes='Original note', suggested_target_concept_id=old.pk,
        suggestion_model_version='v0.4')
    MappingDestinationCandidate.objects.create(mapping=row, target_concept=old,
        target_vocabulary_id=old.vocabulary_id, target_concept_code=old.concept_code, origins=['HT-One'])
    registry = historical_apps()
    receipt = reconcile(registry, connection, payload(multiple))
    assert receipt[0]['outcome'] == ('loaded_multiple' if multiple else 'loaded')
    row.refresh_from_db()
    assert row.status == ('proposed' if multiple else 'approved')
    assert row.origin_system == ('athena-multiple' if multiple else 'athena')
    assert row.target_concept_id == (None if multiple else 201)
    assert (row.reviewed_at is None) == multiple
    assert row.suggested_target_concept_id == old.pk and row.suggestion_model_version == 'v0.4'
    assert row.notes.startswith('Original note\n') and old.concept_code in row.notes
    assert set(row.destination_candidates.values_list('target_concept_id', flat=True)) == ({201, 202} if multiple else {201})
    assert all(c.origins == ['Athena'] for c in row.destination_candidates.all())
    before = (row.notes, row.updated_at, list(row.destination_candidates.values_list('id', flat=True)))
    second = reconcile(registry, connection, payload(multiple))
    assert second[0]['outcome'] == ('already_athena_choices' if multiple else 'already_athena')
    row.refresh_from_db()
    assert (row.notes, row.updated_at, list(row.destination_candidates.values_list('id', flat=True))) == before


@pytest.mark.django_db
def test_snapshot_creates_missing_mappings_concepts_and_references_offline():
    result = reconcile(historical_apps(), connection, payload())
    assert result[0]['outcome'] == 'loaded'
    row = Mapping.objects.get(source_vocabulary_id='ICD10', source_code='C91.10')
    assert row.target_concept_id == 201 and row.status == 'approved'
    assert Concept.objects.get(pk=201).source is None
    assert row.destination_candidates.get().origins == ['Athena']


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [dict(status='approved'), dict(status='rejected'),
    dict(origin_system='curator'), dict(origin='curator', target_concept_id=99999)])
def test_snapshot_preserves_existing_decisions(changes):
    row = Mapping.objects.create(source_vocabulary_id='ICD10', source_code='C91.10', **changes)
    assert reconcile(apps, connection, payload())[0]['outcome'] == 'preserved'
    row.refresh_from_db()
    for key, value in changes.items(): assert getattr(row, key) == value
    assert not Concept.objects.filter(pk=201).exists()


@pytest.mark.django_db
def test_invalid_second_destination_does_not_partially_replace_choices():
    data = payload(True)
    data['concepts']['202']['standard_concept'] = ''
    row = Mapping.objects.create(source_vocabulary_id='ICD10', source_code='C91.10', origin='import')
    assert reconcile(apps, connection, data)[0]['outcome'] == 'conflict'
    row.refresh_from_db()
    assert row.status == 'proposed' and row.target_concept_id is None
    assert not Concept.objects.filter(pk__in=[201,202]).exists()


@pytest.mark.django_db
def test_existing_conflicting_destination_is_preserved():
    existing = ConceptFactory(concept_id=201, concept_code='LOCAL',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'), domain=DomainFactory(domain_id='Condition'))
    assert reconcile(apps, connection, payload())[0]['outcome'] == 'conflict'
    existing.refresh_from_db()
    assert existing.concept_code == 'LOCAL'
    assert not Mapping.objects.filter(source_code='C91.10').exists()


@pytest.mark.django_db
def test_dry_run_has_no_database_mutations_or_write_locks():
    with CaptureQueriesContext(connection) as queries:
        receipt = reconcile(apps, connection, payload(True), dry_run=True)
    assert receipt[0]['outcome'] == 'would_load_multiple'
    sql = [q['sql'].lstrip().upper() for q in queries]
    assert not any(q.startswith(('INSERT', 'UPDATE', 'DELETE')) or 'FOR UPDATE' in q for q in sql)
    assert not Mapping.objects.filter(source_code='C91.10').exists()


def test_packaged_snapshot_has_exact_counts_and_verified_complete_targets():
    data = migration.read_snapshot()
    assert data['mapping_count'] == 11174 and data['concept_count'] == 7947
    assert sum(len(r['target_concept_ids']) == 1 for r in data['mappings']) == 10627
    assert sum(len(r['target_concept_ids']) > 1 for r in data['mappings']) == 547
    assert {r['tier'] for r in data['mappings']} == {'local directory', 'web'}
    for record in data['mappings']:
        assert record['source_vocabulary_id'] == 'ICD10' and record['lookup_vocabulary'] == 'ICD10CM'
        for cid in record['target_concept_ids']:
            target = data['concepts'][str(cid)]
            assert target['standard_concept'] == 'S' and not target['invalid_reason']
            for key in ('vocabulary_id', 'domain_id', 'concept_class_id'):
                assert target[key] in data['metadata'][key]


def test_corrupt_packaged_snapshot_fails_before_database_access(tmp_path, monkeypatch):
    path = tmp_path/'bad.gz'
    path.write_bytes(b'invalid')
    monkeypatch.setattr(migration, 'SNAPSHOT', path)
    with pytest.raises(ValueError, match='checksum'):
        migration.load_mappings(apps, SimpleNamespace(connection=connection))


@pytest.mark.django_db
@pytest.mark.parametrize('dry_run', [False, True])
def test_verified_refresh_corrects_domain_and_retired_concept(dry_run):
    existing = ConceptFactory(concept_id=201, concept_code='SNOMED-201',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Observation'), standard_concept=None,
        invalid_reason='U', source=None)
    data = payload()
    data['refresh_concept_ids'] = [201]
    with CaptureQueriesContext(connection) as queries:
        result = reconcile(historical_apps(), connection, data, dry_run=dry_run)
    assert result[0]['outcome'] == ('would_load' if dry_run else 'loaded')
    existing.refresh_from_db()
    assert existing.domain_id == ('Observation' if dry_run else 'Condition')
    assert existing.standard_concept == (None if dry_run else 'S')
    assert existing.invalid_reason == ('U' if dry_run else None)
    if dry_run:
        assert not any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))
                       or 'FOR UPDATE' in q['sql'].upper() for q in queries)
    else:
        assert Mapping.objects.get(source_code='C91.10').domain_id == 'Condition'


@pytest.mark.django_db
def test_verified_refresh_preserves_conflicting_local_identity():
    existing = ConceptFactory(concept_id=201, concept_code='LOCAL',
        vocabulary=VocabularyFactory(vocabulary_id='SNOMED'),
        domain=DomainFactory(domain_id='Observation'), standard_concept='S')
    data = payload()
    data['refresh_concept_ids'] = [201]
    assert reconcile(historical_apps(), connection, data)[0]['outcome'] == 'conflict'
    existing.refresh_from_db()
    assert existing.concept_code == 'LOCAL' and existing.domain_id == 'Observation'


@pytest.mark.django_db
def test_complete_packaged_migration_offline_and_repeatable():
    registry = historical_apps()
    data = migration.read_snapshot()
    assert len(data['refresh_concept_ids']) == 57
    assert set(map(str, data['refresh_concept_ids'])) == set(data['refresh_evidence'])
    receipts = reconcile(registry, connection, data)
    assert Counter(r['outcome'] for r in receipts) == {'loaded': 10627, 'loaded_multiple': 547}
    assert Mapping.objects.filter(origin_system='athena', status='approved').count() == 10627
    assert Mapping.objects.filter(origin_system='athena-multiple', status='proposed',
                                  target_concept_id__isnull=True).count() == 547
    assert MappingDestinationCandidate.objects.count() == sum(len(r['target_concept_ids']) for r in data['mappings'])
    receipts = reconcile(registry, connection, data)
    assert Counter(r['outcome'] for r in receipts) == {'already_athena': 10627, 'already_athena_choices': 547}
