"""Reconcile hand-mapped ICD-10 SCCM rows against Athena's live STCM table."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.apps import apps as live_apps
from django.db import connection
from django.utils import timezone

from omop_core.data_migrations.athena_icd10_stcm_reconcile import reconcile, summarize
from omop_core.models import (
    Concept, SourceCodeConceptMapping as Mapping, SourceToConceptMap as STCM,
)
from tests.factories import ConceptFactory, DomainFactory, VocabularyFactory


def _stcm(source_code, source_vocab, target_concept, target_vocab='SNOMED'):
    return STCM.objects.create(
        source_code=source_code,
        source_concept=target_concept,  # not critical for this test
        source_vocabulary_id=source_vocab,
        target_concept=target_concept,
        target_vocabulary_id=target_vocab,
        valid_start_date=date(2000, 1, 1),
        valid_end_date=date(2099, 12, 31),
    )


def _mapping(source_code, source_vocab, target_concept, origin_system='HT-One',
             status='approved'):
    return Mapping.objects.create(
        source_code=source_code,
        source_vocabulary_id=source_vocab,
        target_concept=target_concept,
        destination_vocabulary_id=target_concept.vocabulary_id,
        domain_id=target_concept.domain_id,
        omop_table='condition',
        status=status,
        origin='import',
        origin_system=origin_system,
        source='HealthKey',
    )


@pytest.fixture
def condition_domain():
    return DomainFactory(domain_id='Condition', domain_name='Condition')


@pytest.fixture
def snomed_vocab():
    return VocabularyFactory(vocabulary_id='SNOMED', vocabulary_name='SNOMED CT')


@pytest.fixture
def old_target(condition_domain, snomed_vocab):
    """A hand-mapped destination concept."""
    return ConceptFactory(
        concept_id=5001,
        concept_code='OLD-1',
        concept_name='Old Target',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )


@pytest.fixture
def athena_target(condition_domain, snomed_vocab):
    """Athena's destination concept (different from old_target)."""
    return ConceptFactory(
        concept_id=5002,
        concept_code='ATHENA-1',
        concept_name='Athena Target',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )


@pytest.mark.django_db
def test_destination_changed(old_target, athena_target):
    """When Athena disagrees with hand-mapped destination, update it."""
    row = _mapping('A01.0', 'ICD10CM', old_target)
    approved_at = timezone.now() - timedelta(days=2)
    Mapping.objects.filter(pk=row.pk).update(reviewed_at=approved_at)
    _stcm('A01.0', 'ICD10CM', athena_target)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'destination_changed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.concept_id
    assert row.origin_system == 'athena'
    assert row.source == 'Athena'
    assert row.destination_vocabulary_id == 'SNOMED'
    assert row.domain_id == 'Condition'
    assert row.reviewed_at == approved_at
    assert 'destination changed' in row.notes


@pytest.mark.django_db
def test_destination_confirmed(old_target):
    """When Athena agrees with hand-mapped destination, re-attribute only."""
    row = _mapping('B02.0', 'ICD10CM', old_target)
    _stcm('B02.0', 'ICD10CM', old_target)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'reattributed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'athena'
    assert 'destination confirmed' in row.notes


@pytest.mark.django_db
def test_no_stcm_match(old_target):
    """When Athena has no mapping for the code, leave unchanged."""
    row = _mapping('Z99.9', 'ICD10CM', old_target)
    # No STCM row created

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'HT-One'  # unchanged


@pytest.mark.django_db
def test_multiple_targets_skipped(old_target, athena_target, condition_domain, snomed_vocab):
    """When Athena has multiple valid targets, leave the row unchanged."""
    third_target = ConceptFactory(
        concept_id=5003,
        concept_code='ATHENA-2',
        concept_name='Another Athena Target',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='S',
    )
    row = _mapping('C10.0', 'ICD10CM', old_target)
    _stcm('C10.0', 'ICD10CM', athena_target)
    _stcm('C10.0', 'ICD10CM', third_target)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'multiple_targets': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'HT-One'  # unchanged


@pytest.mark.django_db
def test_no_generic_cross_vocabulary_fallback(old_target, athena_target):
    """An ICD10 target does not establish an ICD10CM mapping."""
    row = _mapping('D10.0', 'ICD10CM', old_target)
    _stcm('D10.0', 'ICD10', athena_target)  # different vocab in STCM

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id
    assert row.origin_system == 'HT-One'


@pytest.mark.django_db
def test_ht_one_label_uses_icd10cm_even_when_icd10_has_a_different_target(old_target, athena_target):
    row = _mapping('D10.0', 'ICD10', old_target)
    _stcm('D10.0', 'ICD10', old_target)
    _stcm('D10.0', 'ICD10CM', athena_target)
    assert summarize(reconcile(live_apps, connection)) == {'destination_changed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.pk
    assert 'using ICD10CM:D10.0' in row.notes


@pytest.mark.django_db
def test_non_ht_one_icd10_does_not_use_icd10cm(old_target, athena_target):
    row = _mapping('D10.0', 'ICD10', old_target, origin_system='curator')
    _stcm('D10.0', 'ICD10CM', athena_target)
    assert summarize(reconcile(live_apps, connection)) == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


@pytest.mark.django_db
def test_locked_mapping_and_signoff_are_preserved(old_target, athena_target):
    from patient_portal.models import Identity
    user = Identity.objects.create(issuer='local', sub='stcm-reviewer', uid='stcm-reviewer')
    row = _mapping('D10.0', 'ICD10CM', old_target)
    approved_at = timezone.now() - timedelta(days=3)
    Mapping.objects.filter(pk=row.pk).update(
        locked_by=user, locked_at=timezone.now(), reviewer=user, reviewed_at=approved_at)
    _stcm('D10.0', 'ICD10CM', athena_target)
    assert summarize(reconcile(live_apps, connection)) == {'skipped': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk
    assert row.reviewer_id == row.locked_by_id == user.pk
    assert row.reviewed_at == approved_at


@pytest.mark.django_db
@pytest.mark.parametrize('field,value', [
    ('valid_start_date', date(2099, 1, 1)),
    ('valid_end_date', date(2000, 1, 1)),
    ('invalid_reason', 'D'),
])
def test_invalid_destination_is_not_approved(old_target, athena_target, field, value):
    row = _mapping('D10.0', 'ICD10CM', old_target)
    _stcm('D10.0', 'ICD10CM', athena_target)
    Concept.objects.filter(pk=athena_target.pk).update(**{field: value})
    assert summarize(reconcile(live_apps, connection)) == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk


@pytest.mark.django_db
@pytest.mark.parametrize('changes', [
    {'notes': 'Curator changed this while vocabulary was being read'},
    {'source_vocabulary_id': 'ICD9CM'},
    {'status': 'rejected'},
])
def test_concurrent_curator_edits_are_preserved(old_target, athena_target, changes):
    from omop_core.data_migrations import athena_icd10_stcm_reconcile as loader
    row = _mapping('D10.0', 'ICD10CM', old_target)
    _stcm('D10.0', 'ICD10CM', athena_target)
    original = loader._process_batch

    def edited_batch(*args, **kwargs):
        Mapping.objects.filter(pk=row.pk).update(**changes)
        return original(*args, **kwargs)

    with patch.object(loader, '_process_batch', side_effect=edited_batch):
        assert summarize(reconcile(live_apps, connection)) == {'skipped': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.pk
    for field, value in changes.items():
        assert getattr(row, field) == value


def test_migration_graph_has_one_omop_leaf(settings):
    from django.db.migrations.loader import MigrationLoader
    settings.MIGRATION_MODULES = {}
    loader = MigrationLoader(None)
    leaves = loader.graph.leaf_nodes('omop_core')
    assert len(leaves) == 1
    # Remains valid when later migrations extend this graph.
    assert ('omop_core', '0256_reconcile_icd10_mapped_against_stcm') in loader.graph.forwards_plan(leaves[0])


@pytest.mark.django_db
def test_migration_runs_with_historical_models(old_target, athena_target, settings):
    import importlib
    from types import SimpleNamespace
    from django.db.migrations.loader import MigrationLoader
    settings.MIGRATION_MODULES = {}
    loader = MigrationLoader(connection)
    module = importlib.import_module('omop_core.migrations.0256_reconcile_icd10_mapped_against_stcm')
    registry = loader.project_state(module.Migration.dependencies).apps
    row = _mapping('A01.0', 'ICD10CM', old_target)
    _stcm('A01.0', 'ICD10CM', athena_target)
    module.load_reconciliation(registry, SimpleNamespace(connection=connection))
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.pk
    assert row.origin_system == 'athena'


@pytest.mark.django_db
def test_already_athena_skipped(old_target):
    """Rows already origin_system='athena' are not touched."""
    row = _mapping('E10.0', 'ICD10CM', old_target, origin_system='athena')
    _stcm('E10.0', 'ICD10CM', old_target)

    receipts = reconcile(live_apps, connection)

    assert len(receipts) == 0
    row.refresh_from_db()
    assert row.origin_system == 'athena'  # unchanged


@pytest.mark.django_db
def test_idempotent(old_target, athena_target):
    """Second run finds zero eligible rows."""
    _mapping('F10.0', 'ICD10CM', old_target)
    _stcm('F10.0', 'ICD10CM', athena_target)

    first = reconcile(live_apps, connection)
    assert summarize(first) == {'destination_changed': 1}

    second = reconcile(live_apps, connection)
    assert summarize(second) == {}


@pytest.mark.django_db
def test_nonstandard_stcm_target_ignored(old_target, condition_domain, snomed_vocab):
    """STCM target that is not standard_concept='S' is filtered out."""
    nonstandard = ConceptFactory(
        concept_id=5004,
        concept_code='NS-1',
        concept_name='Non-Standard',
        vocabulary=snomed_vocab,
        domain=condition_domain,
        standard_concept='C',  # not standard
    )
    row = _mapping('G10.0', 'ICD10CM', old_target)
    _stcm('G10.0', 'ICD10CM', nonstandard)

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'no_stcm_match': 1}
    row.refresh_from_db()
    assert row.target_concept_id == old_target.concept_id


@pytest.mark.django_db
def test_proposed_rows_not_eligible(old_target, athena_target):
    """Only approved rows are eligible — proposed rows are skipped."""
    row = _mapping('H10.0', 'ICD10CM', old_target, status='proposed')
    _stcm('H10.0', 'ICD10CM', athena_target)

    receipts = reconcile(live_apps, connection)

    assert len(receipts) == 0
    row.refresh_from_db()
    assert row.origin_system == 'HT-One'  # unchanged


@pytest.mark.django_db
def test_case_insensitive_matching(old_target, athena_target):
    """Source codes match case-insensitively."""
    row = _mapping('j10.0', 'ICD10CM', old_target)  # lowercase
    _stcm('J10.0', 'ICD10CM', athena_target)  # uppercase in STCM

    receipts = reconcile(live_apps, connection)
    summary = summarize(receipts)

    assert summary == {'destination_changed': 1}
    row.refresh_from_db()
    assert row.target_concept_id == athena_target.concept_id
