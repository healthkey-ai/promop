"""Offline deployment applies the same approved-mapping repair on any instance."""
import importlib
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.apps import apps
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone

from omop_core.data_migrations import athena_approved_reconcile_v1 as loader
from omop_core.models import Concept, SourceCodeConceptMapping as Mapping, SourceToConceptMap
from tests.factories import DomainFactory
from tests.test_icd10_stcm_reconcile import (
    _mapping, athena_target, condition_domain, old_target, snomed_vocab,
)

migration = importlib.import_module('omop_core.migrations.0258_reconcile_approved_icd10_from_athena_export')
TODAY = date(2026, 9, 24)


def payload():
    return dict(schema_version=1, as_of='2026-09-21',
                concept_fields=loader.CONCEPT_FIELDS, relationship_fields=loader.RELATIONSHIP_FIELDS,
                source_count=1, target_count=1, relationship_count=1,
                sources=[[123456, 'ICD10CM', 'A01.0', 'Condition', '', '20000101', '20991231', '']],
                targets=[[5002, 'SNOMED', 'ATHENA-1', 'Condition', 'S', '20000101', '20991231', '']],
                relationships=[[123456, 5002, '20000101', '20991231', '']])


def run(data=None, **kwargs):
    return loader.reconcile(apps, connection, data or payload(), today=TODAY, **kwargs)


def historical_state():
    with override_settings(MIGRATION_MODULES={}):
        return MigrationLoader(None).project_state(migration.Migration.dependencies)


@pytest.mark.django_db
def test_actual_migration_operation_uses_historical_models_and_preserves_signoff(old_target, athena_target):
    from patient_portal.models import Identity
    reviewer = Identity.objects.create(issuer='local', sub='offline-reviewer', uid='offline-reviewer')
    row = _mapping('A01.0', 'ICD10CM', old_target)
    Mapping.objects.filter(pk=row.pk).update(reviewer=reviewer, reviewed_at=timezone.now(), notes='Keep this note')
    before = Mapping.objects.values().get(pk=row.pk)
    assert not SourceToConceptMap.objects.exists()
    operation = migration.Migration('0258_reconcile_approved_icd10_from_athena_export', 'omop_core')
    with patch.object(migration, 'read_snapshot', return_value=payload()):
        with CaptureQueriesContext(connection) as queries:
            with connection.schema_editor(atomic=False) as editor:
                operation.apply(historical_state(), editor)
    after = Mapping.objects.values().get(pk=row.pk)
    assert after['target_concept_id'] == athena_target.pk
    assert after['origin_system'] == 'athena'
    assert after['source'] == 'Athena'
    changed = {key for key in before if before[key] != after[key]}
    assert changed <= {'target_concept_id', 'destination_vocabulary_id', 'domain_id', 'omop_table',
                       'origin_system', 'source', 'notes', 'updated_at'}
    assert after['notes'].startswith('Keep this note\nAthena export reconciliation migration 0258')
    assert migration.SHA256 in after['notes']
    writes = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
    assert writes and all(q.startswith('UPDATE "source_code_concept_mapping"') for q in writes)
    assert run() == []


@pytest.mark.django_db
def test_same_destination_is_reattributed_without_patient_rewrite(athena_target):
    row = _mapping('A01.0', 'ICD10CM', athena_target)
    assert loader.summarize(run()) == {'reattributed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.pk and row.origin_system == 'athena'


@pytest.mark.django_db
def test_dry_run_is_select_only(old_target, athena_target):
    row = _mapping('A01.0', 'ICD10CM', old_target)
    before = Mapping.objects.values().get(pk=row.pk)
    with CaptureQueriesContext(connection) as queries:
        assert loader.summarize(run(dry_run=True)) == {'would_change_destination': 1}
    assert all(q['sql'].lstrip().upper().startswith('SELECT') and 'FOR UPDATE' not in q['sql'] for q in queries)
    assert Mapping.objects.values().get(pk=row.pk) == before


@pytest.mark.django_db
@pytest.mark.parametrize('vocab,origin,lookup,expected', [
    ('ICD10', 'HT-One', 'ICD10CM', 'destination_changed'),
    ('ICD10', 'curator', 'ICD10CM', 'not_found'),
    ('ICD10', 'curator', 'ICD10', 'destination_changed'),
    ('ICD10CM', 'HT-One', 'ICD10', 'not_found'),
])
def test_exact_identity_and_only_documented_alias(old_target, athena_target, vocab, origin, lookup, expected):
    data = payload()
    data['sources'][0][1] = lookup
    _mapping(' a01.0 ', vocab, old_target, origin_system=origin)
    assert loader.summarize(run(data)) == {expected: 1}


@pytest.mark.django_db
@pytest.mark.parametrize('section,index,value', [
    ('sources', -1, 'D'), ('sources', -3, '20990101'),
    ('relationships', -1, 'D'), ('relationships', -2, '20000101'),
    ('targets', 4, ''), ('targets', -2, '20000101'),
])
def test_invalid_evidence_is_not_applied(old_target, athena_target, section, index, value):
    data = payload()
    data[section][0][index] = value
    _mapping('A01.0', 'ICD10CM', old_target)
    assert loader.summarize(run(data)) == {'not_found': 1}


@pytest.mark.django_db
def test_future_alternative_is_preserved_and_evaluated_on_deployment_date(old_target, athena_target):
    data = payload()
    data['targets'].append([5001, 'SNOMED', 'OLD-1', 'Condition', 'S', '20000101', '20991231', ''])
    data['target_count'] += 1
    data['relationships'].append([123456, 5001, (TODAY + timedelta(days=1)).isoformat(), '20991231', ''])
    data['relationship_count'] += 1
    _mapping('A01.0', 'ICD10CM', old_target)
    assert loader.summarize(run(data, dry_run=True)) == {'would_change_destination': 1}
    assert loader.summarize(loader.reconcile(apps, connection, data, today=TODAY + timedelta(days=1))) == {'ambiguous_targets': 1}


@pytest.mark.django_db
def test_missing_local_alternative_never_makes_multiple_targets_unique(old_target, athena_target):
    data = payload()
    data['targets'].append([888888, 'SNOMED', 'OTHER', 'Condition', 'S', '20000101', '20991231', ''])
    data['relationships'].append([123456, 888888, '20000101', '20991231', ''])
    data['target_count'] += 1
    data['relationship_count'] += 1
    _mapping('A01.0', 'ICD10CM', old_target)
    assert loader.summarize(run(data)) == {'ambiguous_targets': 1}


@pytest.mark.django_db
def test_duplicate_edges_deduplicate_but_missing_export_target_blocks(old_target, athena_target):
    data = payload()
    data['relationships'].append(list(data['relationships'][0]))
    data['relationship_count'] += 1
    _mapping('A01.0', 'ICD10CM', old_target)
    assert loader.summarize(run(data, dry_run=True)) == {'would_change_destination': 1}
    data['relationships'].append([123456, 888888, '20000101', '20991231', ''])
    data['relationship_count'] += 1
    assert loader.summarize(run(data)) == {'incomplete_evidence': 1}


@pytest.mark.django_db
@pytest.mark.parametrize('changes,expected', [
    ({'concept_code': 'wrong'}, 'local_target_conflict'),
    ({'standard_concept': None}, 'invalid_local_target'),
    ({'source': 'HealthKey'}, 'invalid_local_target'),
    ({'valid_end_date': date(2000, 1, 1)}, 'invalid_local_target'),
])
def test_local_concept_conflicts_preserved(old_target, athena_target, changes, expected):
    _mapping('A01.0', 'ICD10CM', old_target)
    Concept.objects.filter(pk=athena_target.pk).update(**changes)
    assert loader.summarize(run()) == {expected: 1}


@pytest.mark.django_db
def test_domain_conflict_and_missing_target_preserved(old_target, athena_target):
    _mapping('A01.0', 'ICD10CM', old_target)
    Concept.objects.filter(pk=athena_target.pk).update(domain=DomainFactory(domain_id='Observation'))
    assert loader.summarize(run()) == {'local_target_conflict': 1}
    Concept.objects.filter(pk=athena_target.pk).delete()
    assert loader.summarize(run()) == {'missing_local_target': 1}


@pytest.mark.django_db
def test_locked_rows_preserved(old_target, athena_target):
    from patient_portal.models import Identity
    user = Identity.objects.create(issuer='local', sub='offline-lock', uid='offline-lock')
    row = _mapping('A01.0', 'ICD10CM', old_target)
    Mapping.objects.filter(pk=row.pk).update(locked_by=user)
    assert loader.summarize(run()) == {'locked': 1}


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [{'notes': 'Concurrent curator edit'}, {'status': 'rejected'}, {'origin_system': 'athena'}])
def test_concurrent_edits_are_preserved(old_target, athena_target, changes):
    row = _mapping('A01.0', 'ICD10CM', old_target)
    original = loader._batch

    def edit_then_apply(*args, **kwargs):
        Mapping.objects.filter(pk=row.pk).update(**changes)
        return original(*args, **kwargs)

    with patch.object(loader, '_batch', side_effect=edit_then_apply):
        assert loader.summarize(run()) == {'changed_during_audit': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


@pytest.mark.django_db
def test_already_athena_and_unapproved_rows_excluded(old_target, athena_target):
    _mapping('A01.0', 'ICD10CM', athena_target, origin_system='athena')
    _mapping('B01.0', 'ICD10CM', old_target, status='proposed')
    assert run() == []


@pytest.mark.django_db
def test_batches_cover_instance_rows_without_healthkey_ids(old_target, athena_target):
    data = payload()
    data['sources'], data['relationships'] = [], []
    mappings = []
    for index in range(251):
        code = f'EXTERNAL-{index}'
        source = list(payload()['sources'][0])
        source[0], source[2] = 100000 + index, code
        data['sources'].append(source)
        data['relationships'].append([source[0], 5002, '20000101', '20991231', ''])
        mappings.append(Mapping(source_vocabulary_id='ICD10CM', source_code=code, status='approved',
                                origin_system='HealthTree', target_concept_id=old_target.pk))
    data['source_count'] = data['relationship_count'] = 251
    Mapping.objects.bulk_create(mappings)
    assert loader.summarize(run(data)) == {'destination_changed': 251}
    assert run(data) == []


def test_bundled_snapshot_is_complete_and_checksum_verified(tmp_path):
    data = migration.read_snapshot()
    loader.validate(data)
    assert data['source_count'] == 116911
    assert data['relationship_count'] == 148546
    assert {row[1] for row in data['sources']} == {'ICD10', 'ICD10CM'}
    corrupt = tmp_path / 'corrupt.gz'
    corrupt.write_bytes(b'corrupted evidence')
    with patch.object(migration, 'SNAPSHOT', corrupt):
        with pytest.raises(ValueError, match='checksum'):
            migration.read_snapshot()


def test_snapshot_shape_validated_before_database_access():
    data = payload()
    data['target_count'] = 2
    with pytest.raises(ValueError, match='counts'):
        loader.reconcile(None, None, data)


def test_migration_graph_extends_0257_without_replacing_0256():
    with override_settings(MIGRATION_MODULES={}):
        graph = MigrationLoader(None).graph
    leaf, = graph.leaf_nodes('omop_core')
    assert ('omop_core', '0258_reconcile_approved_icd10_from_athena_export') in graph.forwards_plan(leaf)
    assert ('omop_core', '0256_reconcile_icd10_mapped_against_stcm') in graph.forwards_plan(leaf)
